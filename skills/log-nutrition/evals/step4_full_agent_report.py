#!/usr/bin/env python3
"""Report/scorer for Step 4 isolated full-agent A/B replay artifacts.

Consumes an existing run directory. It does not run agents and writes only when
--results-path is provided.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import duckdb

HERE = Path(__file__).resolve().parent
DEFAULT_MANIFEST = HERE / "fixtures" / "step4_full_agent_manifest.json"
PRIMARY_CASES = ["exact-reuse", "recipe-conflict", "typo-fuzzy", "ambiguous", "brand", "replay"]
BASELINE_IDS = {101, 102, 103}
WRITE_WORDS = re.compile(r"\b(wrote|logged|write status|status:\s*wrote)\b", re.I)
CLARIFY_WORDS = re.compile(r"\b(clarif|what .*\?|was this|portions?|quantity|same quantities)\b", re.I | re.S)
PASSTHROUGH_WORDS = re.compile(r"\b(pass(?:ed)? through|already_logged|duplicate replay|idempotent|no duplicate)\b", re.I)


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * pct
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return xs[int(k)]
    return xs[lo] * (hi - k) + xs[hi] * (k - lo)


def latency_summary(values: list[float]) -> dict[str, Any]:
    return {
        "sample_count": len(values),
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
    }


def load_json(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def visible_payload_text(result: dict[str, Any]) -> str:
    payloads = result.get("result", {}).get("payloads", [])
    return "\n\n".join(p.get("text", "") for p in payloads if isinstance(p, dict))


def classify_response(text: str) -> dict[str, bool]:
    negated_write = bool(re.search(r"\b(wrote\s*:\s*no|did not write|not written|not write|no new row)\b", text, re.I))
    return {
        "claimed_write": bool(WRITE_WORDS.search(text)) and not negated_write,
        "clarified": bool(CLARIFY_WORDS.search(text)),
        "passed_through": bool(PASSTHROUGH_WORDS.search(text)),
    }


def get_session_path(result: dict[str, Any]) -> str | None:
    return (
        result.get("result", {})
        .get("meta", {})
        .get("agentMeta", {})
        .get("sessionFile")
    )


def safe_session_label(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    parent = p.parent.parent.name if len(p.parents) >= 2 else "sessions"
    return f"{parent}/sessions/{p.name}"


def _walk_tool_calls(content: Any) -> list[dict[str, Any]]:
    """Return assistant toolCall parts from OpenClaw JSONL content structures."""
    found: list[dict[str, Any]] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "toolCall":
                    found.append(part)
                found.extend(_walk_tool_calls(part.get("content")))
    elif isinstance(content, dict):
        if content.get("type") == "toolCall":
            found.append(content)
        for value in content.values():
            if isinstance(value, (list, dict)):
                found.extend(_walk_tool_calls(value))
    return found


def _call_blob(call: dict[str, Any]) -> str:
    args = call.get("arguments")
    try:
        args_text = json.dumps(args, sort_keys=True)
    except TypeError:
        args_text = str(args)
    return f"{call.get('name') or ''}\n{args_text}".lower()


def _looks_like_file_inspection(command: str) -> bool:
    if "&&" in command or ";" in command:
        return False
    return bool(re.match(r"\s*(test|ls|find|grep|rg|sed|cat|head|tail)\b", command))


def _is_nutrition_retrieve_invocation(command: str) -> bool:
    if "nutrition_retrieve.py" not in command or _looks_like_file_inspection(command):
        return False
    return bool(re.search(r"\b(python3?|uv\s+run\s+python|bunx?)\b", command))


def _is_sql_retrieval(command: str) -> bool:
    return bool(re.search(r"\bselect\b.+\bfrom\b", command, re.I | re.S)) and bool(
        re.search(r"nutrition_log|recipe|food|meal|duckdb|sqlite", command, re.I)
    )


def _is_write_command(command: str) -> bool:
    if _looks_like_file_inspection(command) or re.search(r"\b--help\b|\b-h\b", command):
        return False
    return bool(
        (re.search(r"\b(log_nutrition\.py|quick_log_text\.py)\b", command) and "--json" in command)
        or re.search(r"\binsert\s+into\s+nutrition_log\b", command, re.I)
        or re.search(r"\bingest_nutrition\s*\(", command)
    )


def _is_explicit_http_command(command: str) -> bool:
    return bool(
        re.search(r"\b(curl|wget|http)\b\s+", command)
        or re.search(r"\brequests\.(get|post|request)\s*\(", command)
        or re.search(r"\burllib\.request\.(urlopen|Request)\s*\(", command)
    )


def _is_usda_command(command: str) -> bool:
    return bool(re.search(r"\b(usda|fooddata\s*central|fdc)\b", command, re.I)) and bool(
        re.search(r"\b(curl|wget|http|requests\.|urllib\.|cache|api|fooddata|fdc)\b", command, re.I)
    )


def tool_stats(session_file: str | None) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "available": False,
        "model_rounds": 0,
        "tool_calls_by_type": {},
        "retrieval_calls": 0,
        "usda_calls": 0,
        "web_calls": 0,
        "write_tool_calls": 0,
        "session_label": safe_session_label(session_file),
    }
    if not session_file or not Path(session_file).exists():
        stats["limitation"] = "session JSONL unavailable"
        return stats
    counts: Counter[str] = Counter()
    retrieval = usda = web = write = rounds = 0
    with Path(session_file).open(errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message") or {}
            if msg.get("role") == "assistant" and msg.get("api"):
                rounds += 1
                for call in _walk_tool_calls(msg.get("content")):
                    name = call.get("name") or "unknown"
                    counts[name] += 1
                    blob = _call_blob(call)
                    command = ""
                    args = call.get("arguments")
                    if isinstance(args, dict):
                        command = str(args.get("command") or "")
                    if _is_nutrition_retrieve_invocation(command) or _is_sql_retrieval(command):
                        retrieval += 1
                    if _is_usda_command(blob):
                        usda += 1
                    if name in {"web_search", "browser"} or _is_explicit_http_command(command):
                        web += 1
                    if _is_write_command(command):
                        write += 1
    stats.update({
        "available": True,
        "model_rounds": rounds,
        "tool_calls_by_type": dict(sorted(counts.items())),
        "retrieval_calls": retrieval,
        "usda_calls": usda,
        "web_calls": web,
        "write_tool_calls": write,
    })
    return stats

def db_rows(db_path: Path) -> tuple[list[dict[str, Any]], list[tuple[Any, ...]]]:
    con = duckdb.connect(str(db_path), read_only=True)
    rows = con.execute("select * from nutrition_log order by entry_id").fetchall()
    names = [d[0] for d in con.description]
    out = [dict(zip(names, r)) for r in rows]
    try:
        identities = con.execute("select provider, message_id, entry_id from nutrition_ingest_identities order by provider, message_id").fetchall()
    except Exception:
        identities = []
    con.close()
    return out, identities


def entry_public(row: dict[str, Any]) -> dict[str, Any]:
    keys = ["entry_id", "meal_time", "meal_type", "meal_name", "calories", "protein_g", "carbs_g", "fat_total_g", "source", "ingest_provider", "ingest_message_id"]
    out = {}
    for k in keys:
        v = row.get(k)
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        out[k] = v
    return out


def approx(a: Any, b: Any, tol: float = 0.25) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except Exception:
        return False


def score_case(path_name: str, case: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    case_id = case["id"]
    cdir = run_dir / path_name / case_id
    result = load_json(cdir / "result.json")
    replay_result = load_json(cdir / "replay-result.json") if (cdir / "replay-result.json").exists() else None
    text = visible_payload_text(result)
    if replay_result:
        text += "\n\n[replay]\n" + visible_payload_text(replay_result)
    response_flags = classify_response(text)
    rows, identities = db_rows(cdir / "step4.duckdb")
    new_rows = [r for r in rows if r.get("entry_id") not in BASELINE_IDS]
    expected_mid = case["message_id_template"].format(path=path_name)
    expected = case.get("expected_entry") or {}
    matched = None
    for r in new_rows:
        if expected and expected.get("meal_name_contains", "").lower() in str(r.get("meal_name", "")).lower():
            matched = r
            break
    if not matched and new_rows:
        matched = new_rows[0]

    row_provider_message_id_ok = True
    if case.get("requires_provider_message_id"):
        row_provider_message_id_ok = bool(matched and matched.get("ingest_provider") == "discord" and matched.get("ingest_message_id") == expected_mid)
    receipt_identity_ok = True
    if case.get("requires_provider_message_id"):
        receipt_identity_ok = ("discord", expected_mid, matched.get("entry_id") if matched else None) in identities
    source = str(matched.get("source") if matched else "")
    provenance_ok = True
    if case.get("requires_source_provenance"):
        provenance_ok = bool(source and source != "chat")
    if case.get("requires_published_brand_source"):
        provenance_ok = provenance_ok and bool(re.search(r"chipotle|official|nutrition|https?://", source, re.I))

    expected_action = case["expected_action"]
    action_ok = False
    nutrients_ok = True
    entry_id_ok = True
    false_confirmation = False
    if expected_action == "clarify":
        action_ok = len(new_rows) == 0 and response_flags["clarified"] and not response_flags["claimed_write"]
    else:
        entry_id_ok = bool(matched and int(matched.get("entry_id")) >= int(expected.get("entry_id_min", 0)))
        nutrients_ok = bool(matched) and all(approx(matched.get(k), v) for k, v in expected.items() if k in {"calories", "protein_g", "carbs_g", "fat_total_g"})
        action_ok = bool(matched and len(new_rows) == 1 and entry_id_ok and nutrients_ok)
        if expected_action == "write_then_replay_idempotent":
            action_ok = action_ok and len(new_rows) == 1 and bool(replay_result) and (response_flags["passed_through"] or "no duplicate" in text.lower() or "idempotent" in text.lower())
        if response_flags["claimed_write"] and not matched:
            false_confirmation = True

    duplicates = max(0, len(new_rows) - 1)
    duplicate_provider_ids = 0
    mids = [r.get("ingest_message_id") for r in new_rows if r.get("ingest_message_id")]
    duplicate_provider_ids = len(mids) - len(set(mids))
    # Case correctness is the behavior expected by the prompt plus case-critical
    # safety checks. Provider/message-id and source/provenance are also reported
    # independently so old-path safety regressions remain visible without hiding
    # replay idempotency behavior.
    critical_ok = True
    sequence_or_identity_safety_failure = False
    if case.get("requires_provider_message_id"):
        sequence_or_identity_safety_failure = not (entry_id_ok and row_provider_message_id_ok and receipt_identity_ok)
    if case_id == "exact-reuse":
        critical_ok = row_provider_message_id_ok and receipt_identity_ok
    if case_id == "brand":
        critical_ok = entry_id_ok and provenance_ok and row_provider_message_id_ok and receipt_identity_ok
    if path_name == "new" and case.get("requires_provider_message_id"):
        critical_ok = critical_ok and row_provider_message_id_ok and receipt_identity_ok and provenance_ok
    correctness = bool(action_ok and critical_ok and duplicates == 0)

    sess = get_session_path(result)
    tstats = tool_stats(sess)
    replay_tstats = tool_stats(get_session_path(replay_result)) if replay_result else None
    duration = result.get("result", {}).get("meta", {}).get("durationMs")
    replay_duration = replay_result.get("result", {}).get("meta", {}).get("durationMs") if replay_result else None
    return {
        "case": case_id,
        "result_file": "result.json",
        "replay_result_file": "replay-result.json" if replay_result else None,
        "latency_ms": duration,
        "replay_latency_ms": replay_duration,
        "model_rounds": tstats["model_rounds"] + ((replay_tstats or {}).get("model_rounds") or 0),
        "tool_calls_by_type": dict(Counter(tstats["tool_calls_by_type"]) + Counter((replay_tstats or {}).get("tool_calls_by_type", {}))),
        "retrieval_calls": tstats["retrieval_calls"] + ((replay_tstats or {}).get("retrieval_calls") or 0),
        "usda_calls": tstats["usda_calls"] + ((replay_tstats or {}).get("usda_calls") or 0),
        "web_calls": tstats["web_calls"] + ((replay_tstats or {}).get("web_calls") or 0),
        "write_tool_calls": tstats["write_tool_calls"] + ((replay_tstats or {}).get("write_tool_calls") or 0),
        "db_write_rows": len(new_rows),
        "duplicates": duplicates + duplicate_provider_ids,
        "correct": correctness,
        "action_ok": action_ok,
        "nutrients_ok": nutrients_ok,
        "row_provider_message_id_ok": row_provider_message_id_ok,
        "receipt_identity_ok": receipt_identity_ok,
        "sequence_or_identity_safety_failure": sequence_or_identity_safety_failure,
        "source_provenance_ok": provenance_ok,
        "clarification": response_flags["clarified"],
        "pass_through": response_flags["passed_through"],
        "false_confirmation": false_confirmation,
        "new_entries": [entry_public(r) for r in new_rows],
        "observed_issue": "manual/anomalous entry_id allocation or missing durable identity receipt" if sequence_or_identity_safety_failure else None,
        "session_jsonl": tstats["session_label"],
        "replay_session_jsonl": (replay_tstats or {}).get("session_label") if replay_tstats else None,
    }


def summarize_path(path_name: str, cases: list[dict[str, Any]], run_dir: Path) -> dict[str, Any]:
    scored = [score_case(path_name, c, run_dir) for c in cases]
    latencies = []
    for s in scored:
        if s["latency_ms"] is not None:
            latencies.append(float(s["latency_ms"]))
        if s["replay_latency_ms"] is not None:
            latencies.append(float(s["replay_latency_ms"]))
    tool_counts = Counter()
    for s in scored:
        tool_counts.update(s["tool_calls_by_type"])
    primary_correct = sum(1 for s in scored if s["correct"])
    write_cases = [s for s in scored if s["case"] in {"exact-reuse", "recipe-conflict", "brand", "replay"}]
    return {
        "latency": latency_summary(latencies),
        "model_rounds": sum(s["model_rounds"] for s in scored),
        "tool_calls_by_type": dict(sorted(tool_counts.items())),
        "retrieval_calls": sum(s["retrieval_calls"] for s in scored),
        "usda_calls": sum(s["usda_calls"] for s in scored),
        "web_calls": sum(s["web_calls"] for s in scored),
        "writes": sum(s["db_write_rows"] for s in scored),
        "duplicates": sum(s["duplicates"] for s in scored),
        "correctness": {"correct": primary_correct, "total": len(scored), "rate": primary_correct / len(scored)},
        "clarification": {"observed": sum(1 for s in scored if s["clarification"]), "expected": 2},
        "pass_through": {"observed": sum(1 for s in scored if s["pass_through"]), "expected_min": 1},
        "false_confirmations": sum(1 for s in scored if s["false_confirmation"]),
        "write_tool_calls": sum(s["write_tool_calls"] for s in scored),
        "row_provider_message_id_integrity": {"ok": sum(1 for s in write_cases if s["row_provider_message_id_ok"]), "total": len(write_cases)},
        "receipt_identity_integrity": {"ok": sum(1 for s in write_cases if s["receipt_identity_ok"]), "total": len(write_cases)},
        "source_provenance_integrity": {"ok": sum(1 for s in write_cases if s["source_provenance_ok"]), "total": len(write_cases)},
        "cases": scored,
    }


def build_report(run_dir: Path, manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    cases = manifest["cases"]
    by_path = {p: summarize_path(p, cases, run_dir) for p in manifest["paths"]}
    report = {
        "schema_version": 1,
        "artifact": "log-nutrition Step 4 isolated full-agent A/B replay evaluation",
        "run_dir_label": "external run directory supplied at evaluation time (not serialized)",
        "manifest": "evals/fixtures/step4_full_agent_manifest.json",
        "paths": by_path,
        "score_summary": {
            "old_correctness": by_path["old"]["correctness"],
            "new_correctness": by_path["new"]["correctness"],
            "new_meets_gate": by_path["new"]["correctness"]["correct"] == 6
            and by_path["new"]["correctness"]["total"] == 6
            and by_path["new"]["duplicates"] == 0
            and by_path["new"]["false_confirmations"] == 0
            and by_path["new"]["row_provider_message_id_integrity"] == {"ok": 4, "total": 4}
            and by_path["new"]["receipt_identity_integrity"] == {"ok": 4, "total": 4}
            and by_path["new"]["source_provenance_integrity"] == {"ok": 4, "total": 4},
            "old_brand_sequence_or_identity_safety_failure": any(
                c["case"] == "brand" and c["sequence_or_identity_safety_failure"]
                for c in by_path["old"]["cases"]
            ),
            "old_exact_reuse_safety_failure": any(
                c["case"] == "exact-reuse" and c["sequence_or_identity_safety_failure"]
                for c in by_path["old"]["cases"]
            ),
        },
        "limitations": [
            "Reporter scores completed artifacts only; it does not rerun agents or validate unpublished hidden reasoning.",
            "Tool-call categories are counted from assistant toolCall records and their argument payloads; they are auditable counts, not a semantic proof of every sub-operation inside a script.",
            "Session JSONL labels are sanitized to agent/session file names; raw trajectories and encrypted reasoning are not copied into this artifact.",
            "USDA/web calls executed inside shell scripts can only be counted when explicit in assistant tool-call arguments.",
            "The manifest intentionally omits production, local host, temporary DB, and session paths.",
            "This is a small-n isolated replay comparison; it is useful for regression evidence, not a population estimate.",
            "The old baseline may be contaminated by manual/anomalous ID allocation and missing durable identity machinery; those safety failures are reported separately from row-column provider/message evidence.",
        ],
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", nargs="?", default="/tmp/food-journal-step4-full-agent/runs", help="Completed Step 4 run directory containing old/ and new/ subdirectories")
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--results-path", help="Optional path to write JSON report. Defaults to stdout only.")
    ap.add_argument("--pretty", action="store_true", help="Pretty-print JSON (default on stdout).")
    args = ap.parse_args()
    report = build_report(Path(args.run_dir), Path(args.manifest))
    pretty = args.pretty or not args.results_path
    text = json.dumps(report, indent=2 if pretty else None, sort_keys=False) + "\n"
    if args.results_path:
        Path(args.results_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.results_path).write_text(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
