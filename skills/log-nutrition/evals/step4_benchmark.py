#!/usr/bin/env python3
"""Step 4 deterministic before/after benchmark for log-nutrition.

This is intentionally not a full-agent replay. It compares a labeled frozen
pre-retrieval operation (ILIKE history/recipe lookup only) with the current
read-only nutrition_retrieve plus the shared writer on executable fixtures.
All mutable work happens in isolated temporary DuckDB files.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = REPO_ROOT / "skills" / "log-nutrition"
EVAL_DIR = SKILL_DIR / "evals"
TESTS_DIR = SKILL_DIR / "tests"
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

from bootstrap.env import db_path as live_db_path  # noqa: E402
from nutrition_ingest import ingest_nutrition, migrate_database, resolve_and_ingest_quick_text, writable_database  # noqa: E402
from nutrition_retrieve import retrieve_nutrition  # noqa: E402

REPLAY_FIXTURE = EVAL_DIR / "fixtures" / "replay-corpus.json"
RETRIEVAL_FIXTURE = TESTS_DIR / "fixtures" / "retrieval_corpus.json"
P0_FIXTURE = EVAL_DIR / "fixtures" / "p0-executable.json"
TRACKED_RESULTS = EVAL_DIR / "step4_results.json"


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _item(name: str, portion_g: float, calories: float, protein: float, carbs: float, fat: float, aliases: list[str] | None = None) -> dict[str, Any]:
    item = {"item": name, "portion_g": portion_g, "calories": calories, "protein_g": protein, "carbs_g": carbs, "fat_total_g": fat}
    if aliases:
        item["aliases"] = aliases
    return item


def create_empty_db(tmp: Path, name: str = "step4.duckdb") -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    db = tmp / name
    migrate_database(db)
    return db


def _provision_history_row(db: Path, payload: dict[str, Any], message_id: str) -> int:
    """Provision isolated history through the central locked ingest boundary."""
    result = ingest_nutrition(db, payload, identity=("step4-fixture", message_id))
    return int(result["result"]["entry"]["entry_id"])


def create_isolated_db(tmp: Path) -> tuple[Path, Path]:
    db = create_empty_db(tmp)
    profile = tmp / "user-profile.yaml"
    breakfast_items = [
        _item("baguette", 30, 82, 2.7, 16.8, 0.5, ["法棍"]),
        _item("avocado", 75, 120, 1.5, 6.4, 11.0, ["牛油果"]),
        _item("hard-boiled egg", 50, 78, 6.3, 0.6, 5.3, ["白煮蛋", "egg"]),
        _item("black coffee", 240, 2, 0.3, 0, 0, ["黑咖啡", "coffee"]),
    ]
    lunch_items = [_item("rice", 100, 130, 2.4, 28, 0.3), _item("chicken", 120, 198, 37, 0, 4.3)]
    _provision_history_row(db, {
        "meal_time": "2026-08-16T08:05:00", "meal_type": "breakfast",
        "meal_name": "Baguette avocado egg coffee", "meal_description": "habitual multilingual breakfast",
        "food_items": breakfast_items, "calories": 282, "protein_g": 10.8,
        "carbs_g": 23.8, "fat_total_g": 16.8, "source": "fixture-history",
    }, "isolated-history-breakfast")
    _provision_history_row(db, {
        "meal_time": "2026-08-17T12:00:00", "meal_type": "lunch",
        "meal_name": "Chicken rice bowl", "meal_description": "ambiguous prior bowl",
        "food_items": lunch_items, "calories": 328, "protein_g": 39.4,
        "carbs_g": 28, "fat_total_g": 4.6, "source": "fixture-history",
    }, "isolated-history-lunch")
    _provision_history_row(db, {
        "meal_time": "2026-08-17T08:10:00", "meal_type": "breakfast",
        "meal_name": "Morning plate", "meal_description": "historical meal with the same name as a recipe",
        "food_items": [_item("egg", 50, 78, 6.3, 0.6, 5.3), _item("rice", 100, 130, 2.4, 28, 0.3)],
        "calories": 208, "protein_g": 8.7, "carbs_g": 28.6, "fat_total_g": 5.6,
        "source": "fixture-history",
    }, "isolated-history-morning-plate")
    with writable_database(db, allow_create=False) as conn:
        conn.execute("""INSERT INTO recipes (name, description, food_items, total_calories, total_protein_g, total_carbs_g, total_fat_g)
          VALUES ('Morning plate', 'Canonical fixture recipe', ?, 300, 20, 30, 10)""", [json.dumps([_item("egg", 50, 78, 6.3, 0.6, 5.3)])])
        conn.execute("CREATE TABLE IF NOT EXISTS recipe_aliases (alias_id INTEGER PRIMARY KEY, recipe_id INTEGER NOT NULL, alias VARCHAR NOT NULL)")
        rid = conn.execute("SELECT id FROM recipes WHERE name='Morning plate' ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.execute("INSERT INTO recipe_aliases VALUES (1, ?, 'Alex breakfast')", [rid])
    profile.write_text("nutrition_defaults:\n  egg: hard-boiled\n  coffee:\n    kind: black filtered\n    additions: none\n", encoding="utf-8")
    return db, profile


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _terms_from_text(text: str) -> list[str]:
    return [part.strip(" ,，.;:!?()[]'") for part in text.replace("，", " ").replace(",", " ").split() if part.strip(" ,，.;:!?()[]'")]


def old_pre_retrieval_lookup(db: Path, query: dict[str, Any] | str) -> dict[str, Any]:
    """Frozen old op: SQL ILIKE history/recipe lookup only; no parser/agent."""
    text = query if isinstance(query, str) else query.get("text") or " ".join(query.get("terms") or [])
    context = {} if isinstance(query, str) else (query.get("context") or {})
    if context.get("has_image") or context.get("has_photo"):
        return {"status": "ok", "suggested_action": "pass_through", "candidates": [], "retrieval_calls": 0, "writes": 0, "usda_web_calls": None, "usda_web_calls_reason": "full old-agent USDA/web behavior is unavailable in this deterministic SQL-only harness"}
    terms = _terms_from_text(str(text))[:6]
    conn = duckdb.connect(str(db), read_only=True)
    candidates: list[dict[str, Any]] = []
    try:
        for term in terms:
            like = f"%{term}%"
            for row in conn.execute("SELECT entry_id, meal_name, meal_time FROM nutrition_log WHERE meal_name ILIKE ? OR food_items ILIKE ? ORDER BY meal_time DESC LIMIT 3", [like, like]).fetchall():
                candidates.append({"candidate_type": "historical_meal", "label": row[1], "provenance": {"table": "nutrition_log", "entry_id": row[0]}, "meal_time": str(row[2])})
            for row in conn.execute("SELECT id, name FROM recipes WHERE name ILIKE ? OR food_items ILIKE ? ORDER BY name LIMIT 3", [like, like]).fetchall():
                candidates.append({"candidate_type": "recipe", "label": row[1], "provenance": {"table": "recipes", "recipe_id": row[0]}})
    finally:
        conn.close()
    # De-dupe in query order. Old workflow was candidate-only; never writes.
    seen: set[tuple[str, str]] = set()
    unique = []
    for c in candidates:
        key = (c["candidate_type"], c["label"].casefold())
        if key not in seen:
            seen.add(key); unique.append(c)
    return {"status": "ok", "suggested_action": "needs_agent_decision" if unique else "no_match", "candidates": unique[:8], "retrieval_calls": int(bool(terms)), "writes": 0, "usda_web_calls": None, "usda_web_calls_reason": "full old-agent USDA/web behavior is unavailable in this deterministic SQL-only harness"}


def _matches_expectation(result: dict[str, Any], expect: dict[str, Any]) -> dict[str, Any]:
    candidates = result.get("candidates") or []
    ok = True
    failures: list[str] = []
    if "top_action" in expect and result.get("suggested_action") != expect["top_action"]:
        ok = False; failures.append("top_action")
    if "candidate_type" in expect:
        found = any(c.get("candidate_type") == expect["candidate_type"] and ("label" not in expect or c.get("label", "").casefold() == expect["label"].casefold()) and ("match_type" not in expect or c.get("match_type") == expect["match_type"]) and ("suggested_action" not in expect or c.get("suggested_action") == expect["suggested_action"]) for c in candidates)
        if not found:
            ok = False; failures.append("candidate")
    if expect.get("no_write_recommendation") and any(c.get("suggested_action") in {"write", "log", "auto_log"} for c in candidates):
        ok = False; failures.append("unsafe_write_recommendation")
    if expect.get("first_candidate_type") and (not candidates or candidates[0].get("candidate_type") != expect["first_candidate_type"]):
        ok = False; failures.append("first_candidate_type")
    if expect.get("also_candidate_type") and not any(c.get("candidate_type") == expect["also_candidate_type"] for c in candidates):
        ok = False; failures.append("also_candidate_type")
    if expect.get("reason") and expect["reason"] not in result.get("reason_codes", []):
        ok = False; failures.append("reason")
    return {"correct": ok, "failures": failures}


def _safe_no_write(result: dict[str, Any]) -> bool:
    return not any(c.get("suggested_action") in {"write", "log", "auto_log"} for c in (result.get("candidates") or []))


def _has_candidate(result: dict[str, Any], candidate_type: str, *, label: str | None = None, match_type: str | None = None) -> bool:
    for c in result.get("candidates") or []:
        if c.get("candidate_type") != candidate_type:
            continue
        if label is not None and c.get("label", "").casefold() != label.casefold():
            continue
        if match_type is not None and c.get("match_type") != match_type:
            continue
        return True
    return False


def _score_replay_result(case: dict[str, Any], result: dict[str, Any], *, linked_p0: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score one replay fixture against explicit deterministic expectations.

    These are local-harness expectations, not claims about a full OpenClaw agent:
    `needs_clarification` and `published_nutrition_lookup_required` are observed
    as `needs_agent_decision`; `photo_workflow` is observed as `pass_through`;
    delivery replay outcomes are scored from linked isolated-writer P0 evidence.
    """
    outcome = case.get("expected", {}).get("outcome")
    action = result.get("suggested_action")
    failures: list[str] = []

    if outcome == "needs_structured_payload":
        if action != "needs_agent_decision" or not _safe_no_write(result):
            failures.append("expected_agent_decision_without_write")
    elif outcome == "ingredient_reuse":
        if action != "needs_agent_decision" or not (_has_candidate(result, "ingredient_basis") or _has_candidate(result, "historical_meal")):
            failures.append("expected_local_reuse_candidates")
    elif outcome == "pass_to_agent_pending_step_2":
        if action not in {"needs_agent_decision", "no_match"} or not _safe_no_write(result):
            failures.append("expected_safe_agent_handoff")
    elif outcome == "needs_clarification":
        if action != "needs_agent_decision" or not _safe_no_write(result):
            failures.append("expected_clarification_as_agent_decision")
    elif outcome == "exact_recipe_only":
        if action != "needs_agent_decision" or not _has_candidate(result, "recipe", label="Example breakfast", match_type="recipe_exact_name"):
            failures.append("expected_exact_recipe_candidate")
    elif outcome == "recipe_precedes_history":
        candidates = result.get("candidates") or []
        if action != "needs_agent_decision" or not candidates or candidates[0].get("candidate_type") != "recipe":
            failures.append("expected_recipe_first")
    elif outcome == "published_nutrition_lookup_required":
        if action != "needs_agent_decision" or "brand_or_restaurant_requires_external_source" not in result.get("reason_codes", []):
            failures.append("expected_external_source_reason")
    elif outcome == "photo_workflow":
        if action != "pass_through" or "visual_input_requires_agent" not in result.get("reason_codes", []):
            failures.append("expected_visual_pass_through")
    elif outcome in {"durable_result_replayed", "one_committed_result_replayed", "stored_result_replayed"}:
        if not linked_p0:
            failures.append("missing_linked_p0_writer_evidence")
        elif not linked_p0.get("correct") or linked_p0.get("writes") != 1 or linked_p0.get("duplicates") != 0:
            failures.append("linked_p0_writer_evidence_failed")
    else:
        failures.append(f"unscored_outcome:{outcome}")

    return {
        "correct": not failures,
        "failures": failures,
        "expected_outcome": outcome,
        "observed_action": action,
        "observed_clarification": action == "needs_agent_decision",
        "expected_clarification": outcome in {"needs_structured_payload", "needs_clarification", "published_nutrition_lookup_required"},
        "observed_pass_through": action == "pass_through",
        "expected_pass_through": outcome == "photo_workflow",
        "linked_p0_case": linked_p0.get("id") if linked_p0 else None,
    }


def _counts(db: Path) -> dict[str, int]:
    conn = duckdb.connect(str(db), read_only=True)
    try:
        tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
        def count(table: str, where: str = "") -> int:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]) if table in tables else 0
        return {"nutrition_rows": count("nutrition_log"), "receipts": count("nutrition_ingest_receipts"), "ledgers": count("nutrition_ingest_identities"), "anchors": count("nutrition_log", "WHERE ingest_provider IS NOT NULL AND ingest_message_id IS NOT NULL")}
    finally:
        conn.close()


def _run_p0_case(db: Path, case: dict[str, Any]) -> dict[str, Any]:
    if case.get("seed"):
        for index, seed in enumerate(case.get("seed", [])):
            payload = {key: value for key, value in seed.items() if key != "entry_id"}
            if isinstance(payload.get("meal_time"), str):
                payload["meal_time"] = payload["meal_time"].replace("+00:00", "")
                if "T" not in payload["meal_time"]:
                    payload["meal_time"] = payload["meal_time"].replace(" ", "T")
            _provision_history_row(db, payload, f"{case['id']}-seed-{index}")
    before = _counts(db)
    if case["operation"] == "quick":
        result = resolve_and_ingest_quick_text(db, copy.deepcopy(case["payload"]), defaults_loaded=False, data_dir=str(db.parent))
    elif case["operation"] == "ingest_twice":
        first = ingest_nutrition(db, copy.deepcopy(case["payload"]))
        second = ingest_nutrition(db, copy.deepcopy(case["retry_payload"]))
        result = {"first": first, "second": second, "status": first.get("status")}
    else:
        raise ValueError(f"unsupported P0 operation: {case['operation']}")
    after = _counts(db)
    expected = case.get("expected", {})
    correct = True
    if "nutrition_rows" in expected and after["nutrition_rows"] != expected["nutrition_rows"]:
        correct = False
    duplicates = max(0, after["nutrition_rows"] - before["nutrition_rows"] - (1 if expected.get("nutrition_rows", after["nutrition_rows"]) > before["nutrition_rows"] else 0))
    return {"result_status": result.get("status"), "correct": correct, "writes": max(0, after["nutrition_rows"] - before["nutrition_rows"]), "duplicates": duplicates, "counts_after": after}


def _percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"sample_count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None}
    values = sorted(values)
    def pct(p: float) -> float:
        if len(values) == 1:
            return values[0]
        k = (len(values) - 1) * p
        lo = int(k); hi = min(lo + 1, len(values) - 1)
        return values[lo] + (values[hi] - values[lo]) * (k - lo)
    return {"sample_count": len(values), "p50_ms": round(pct(0.50), 3), "p95_ms": round(pct(0.95), 3), "p99_ms": round(pct(0.99), 3)}


def run_benchmark(*, repetitions: int = 31) -> dict[str, Any]:
    live = live_db_path()
    live_before = _sha256(live)
    replay_cases = _load(REPLAY_FIXTURE)["cases"]
    retrieval_cases = _load(RETRIEVAL_FIXTURE)["cases"]
    p0_cases = _load(P0_FIXTURE)["cases"]

    with tempfile.TemporaryDirectory(prefix="nutrition_step4_") as raw_tmp:
        tmp = Path(raw_tmp)
        db, profile = create_isolated_db(tmp)
        rows: list[dict[str, Any]] = []
        timings = {"old": [], "current_retrieval": [], "current_writer": []}
        aggregates = {
            "old": {"retrieval_calls": 0, "writes": 0, "duplicates": 0, "decision_correct": 0, "decision_total": 0, "candidate_correct": 0, "candidate_total": 0, "replay_correct": 0, "replay_total": 0, "clarification_expected": 0, "clarification_observed": 0, "pass_through_expected": 0, "pass_through_observed": 0, "unsafe_fuzzy_writes": 0, "recipe_precedence_errors": 0, "local_benchmark_network_calls": 0, "full_agent_usda_web_calls": None},
            "current": {"retrieval_calls": 0, "writes": 0, "duplicates": 0, "decision_correct": 0, "decision_total": 0, "candidate_correct": 0, "candidate_total": 0, "replay_correct": 0, "replay_total": 0, "clarification_expected": 0, "clarification_observed": 0, "pass_through_expected": 0, "pass_through_observed": 0, "unsafe_fuzzy_writes": 0, "recipe_precedence_errors": 0, "local_benchmark_network_calls": 0, "full_agent_usda_web_calls": None},
        }

        # Stable microbench repetitions over deterministic retrieval fixtures.
        for _ in range(repetitions):
            for case in retrieval_cases:
                query = dict(case["query"])
                if case.get("use_profile"):
                    query["profile_path"] = str(profile)
                t0 = time.perf_counter(); old_pre_retrieval_lookup(db, query); timings["old"].append((time.perf_counter() - t0) * 1000)
                t0 = time.perf_counter(); retrieve_nutrition(db, query, profile_path=query.get("profile_path")); timings["current_retrieval"].append((time.perf_counter() - t0) * 1000)

        # Executable P0 writer fixtures run on fresh isolated DBs for every
        # repetition to produce honest writer latency samples. Correctness is
        # scored once per fixture, from the first isolated execution only.
        p0_score_by_id: dict[str, dict[str, Any]] = {}
        for rep in range(repetitions):
            for case in p0_cases:
                case_tmp = tmp / f"p0_{rep}_{case['id']}"
                p0_db = create_empty_db(case_tmp)
                t0 = time.perf_counter(); writer = _run_p0_case(p0_db, case); timings["current_writer"].append((time.perf_counter() - t0) * 1000)
                if rep == 0:
                    writer = {"id": case["id"], **writer}
                    p0_score_by_id[case["id"]] = writer
                    aggregates["current"]["writes"] += writer["writes"]
                    aggregates["current"]["duplicates"] += writer["duplicates"]
                    aggregates["current"]["decision_correct"] += int(writer["correct"])
                    aggregates["current"]["decision_total"] += 1
                    rows.append({"fixture_set": "p0_executable", "id": case["id"], "scored_once_latency_repetitions": repetitions, **writer})

        delivery_writer_evidence = p0_score_by_id.get("sequential-replay")

        # Report and score one correctness row per replay fixture.
        for case in replay_cases:
            if case["kind"] == "attachment":
                query = {"text": "log this", "context": {"has_image": True}}
            elif "input" in case:
                query = {"text": case["input"]}
            else:
                # Delivery/idempotency replay fixtures exercise the writer in
                # p0-executable.json; here we only include their deterministic
                # retrieval/pass-through surface without inventing old agent text.
                query = {"text": case.get("message_id") or case["id"]}
            old = old_pre_retrieval_lookup(db, query)
            current = retrieve_nutrition(db, query)
            linked_p0 = delivery_writer_evidence if case["kind"] == "delivery" else None
            old_score = _score_replay_result(case, old, linked_p0=linked_p0)
            current_score = _score_replay_result(case, current, linked_p0=linked_p0)
            for name, result in (("old", old), ("current", current)):
                score = old_score if name == "old" else current_score
                aggregates[name]["retrieval_calls"] += result.get("retrieval_calls", 1 if name == "current" else 0)
                aggregates[name]["replay_correct"] += int(score["correct"])
                aggregates[name]["replay_total"] += 1
                aggregates[name]["decision_correct"] += int(score["correct"])
                aggregates[name]["decision_total"] += 1
                aggregates[name]["clarification_expected"] += int(score["expected_clarification"])
                aggregates[name]["clarification_observed"] += int(score["observed_clarification"])
                aggregates[name]["pass_through_expected"] += int(score["expected_pass_through"])
                aggregates[name]["pass_through_observed"] += int(score["observed_pass_through"])
                aggregates[name]["unsafe_fuzzy_writes"] += 0
            rows.append({"fixture_set": "replay", "id": case["id"], "old_score": old_score, "current_score": current_score, "old_action": old.get("suggested_action"), "current_action": current.get("suggested_action"), "model_calls": None, "model_calls_reason": "full-agent/model replay unavailable in deterministic harness"})

        # Retrieval corpus correctness.
        for case in retrieval_cases:
            query = dict(case["query"])
            if case.get("use_profile"):
                query["profile_path"] = str(profile)
            old = old_pre_retrieval_lookup(db, query)
            current = retrieve_nutrition(db, query, profile_path=query.get("profile_path"))
            old_match = _matches_expectation(old, case["expect"])
            cur_match = _matches_expectation(current, case["expect"])
            aggregates["old"]["candidate_correct"] += int(old_match["correct"])
            aggregates["current"]["candidate_correct"] += int(cur_match["correct"])
            aggregates["old"]["candidate_total"] += 1
            aggregates["current"]["candidate_total"] += 1
            if "top_action" in case["expect"]:
                aggregates["old"]["decision_correct"] += int(old.get("suggested_action") == case["expect"]["top_action"])
                aggregates["current"]["decision_correct"] += int(current.get("suggested_action") == case["expect"]["top_action"])
                aggregates["old"]["decision_total"] += 1
                aggregates["current"]["decision_total"] += 1
            aggregates["old"]["retrieval_calls"] += old.get("retrieval_calls", 0)
            aggregates["current"]["retrieval_calls"] += current.get("retrieval_calls", 1)
            # Exact recipe must sort before historical meal in conflict.
            if case["id"] == "recipe_history_conflict":
                if not current.get("candidates") or current["candidates"][0].get("candidate_type") != "recipe":
                    aggregates["current"]["recipe_precedence_errors"] += 1
            rows.append({"fixture_set": "retrieval", "id": case["id"], "old_correct": old_match, "current_correct": cur_match, "old_candidates": len(old.get("candidates", [])), "current_candidates": len(current.get("candidates", []))})

    live_after = _sha256(live)
    return {
        "schema_version": 1,
        "benchmark": "log-nutrition-step4-deterministic",
        "fixture_counts": {"replay": len(replay_cases), "retrieval": len(retrieval_cases), "p0_executable": len(p0_cases)},
        "repetitions": repetitions,
        "semantics": {
            "decision_correct": "explicit numerator/denominator over scored replay fixtures, retrieval fixtures with top_action, and P0 writer fixtures scored once; retrieval fixtures without top_action are excluded from decision_total and only affect candidate correctness",
            "retrieval_calls": "local deterministic function invocations that perform or represent local lookup during scored replay/retrieval operations; warmup/timing-loop calls are excluded",
            "latency_samples": "retrieval latency uses all retrieval fixture timing repetitions; writer latency uses every P0 case on a fresh isolated DB for each repetition",
            "network_calls": "local deterministic benchmark network calls are expected and reported as zero; full-agent USDA/web calls are unavailable/null because no full agent is run",
        },
        "latency": {"old_pre_retrieval_lookup": _percentiles(timings["old"]), "current_nutrition_retrieve": _percentiles(timings["current_retrieval"]), "current_shared_writer_p0": _percentiles(timings["current_writer"])},
        "aggregates": aggregates,
        "unavailable": {
            "model_calls": {"value": None, "reason": "deterministic harness does not run an LLM/full agent"},
            "full_agent_tool_calls": {"value": None, "reason": "OpenClaw full-agent replay is outside this deterministic scope"},
            "visible_response_latency": {"value": None, "reason": "no Discord/user-visible send occurs in isolated-temp-DB benchmark"},
        },
        "live_db_integrity": {"sha256_before": live_before, "sha256_after": live_after, "unchanged": live_before == live_after},
        "remaining_full_agent_replay_gap": "Need a small OpenClaw/Discord agent replay to measure actual model rounds, agent tool calls, and visible-response latency; this deterministic harness can only measure local retrieval/writer operations and fixture correctness.",
        "recommended_full_agent_sample_plan": [
            "Run 12 total canary-style full-agent replays, not 340 turns: 4 repeated/simple local-history meals, 3 recipe/alias/conflict cases, 2 ambiguity/clarification cases, 2 brand/photo pass-through cases, 1 delivery replay/idempotency case.",
            "Use fixed prompts from the fixture corpus, shadow or isolated DB only, record model rounds/tool calls/visible latency, and inspect every transcript for wrong writes before expanding.",
            "If all 12 pass, run 20-30 real Food Journal canary messages as already planned; report binomial uncertainty rather than claiming production p99 from a tiny sample.",
        ],
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Step 4 deterministic benchmark")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write-results", action="store_true", help="write tracked evals/step4_results.json")
    group.add_argument("--results-path", type=Path, help="write JSON to an explicit artifact path")
    parser.add_argument("--repetitions", type=int, default=31, help="microbench repetitions per retrieval fixture")
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        parser.error("--repetitions must be >= 1")
    result = run_benchmark(repetitions=args.repetitions)
    out = args.results_path or (TRACKED_RESULTS if args.write_results else None)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Step 4 results saved to: {out}")
    else:
        print(json.dumps({k: result[k] for k in ("fixture_counts", "latency", "aggregates", "unavailable", "live_db_integrity")}, ensure_ascii=False, indent=2, sort_keys=True))
        print("Results JSON not written by default; use --results-path PATH or --write-results.")
    return 0 if result["live_db_integrity"]["unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
