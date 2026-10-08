#!/usr/bin/env python3
"""Deterministic, network-free, read-only nutrition evidence packets."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import duckdb

from bootstrap.env import db_path as default_db_path, user_profile_path
from nutrition_retrieve import NUTRIENT_FIELDS, RetrievalError, retrieve_nutrition

KB_RECIPE_ROOT = Path("~/knowledge-base/wiki/topics/recipes").expanduser()
MAX_KB_MATCHES = 3
MAX_KB_PACKET_LINES = 120
MAX_KB_PACKET_CHARS = 12000
MAX_KB_PACKET_BYTES = 16000
ENTRY_FIELD_ALLOWLIST = {
    "item", "name", "aliases", "portion_g", "portion", "quantity", "unit", "serving_size",
    "source", "brand", "label", "basis", *NUTRIENT_FIELDS,
}
ENTRY_PROVENANCE_FIELDS = {"meal_time", "meal_type", "meal_name", "source"}
ENTRY_REQUEST_FIELD_ALLOWLIST = ENTRY_FIELD_ALLOWLIST | ENTRY_PROVENANCE_FIELDS
# Omitted fields retain a useful compatibility path without pretending every
# item carries all alternative portion aliases or every optional nutrient.
DEFAULT_ENTRY_REQUEST_FIELDS = ["item", "calories", "protein_g", "carbs_g", "fat_total_g"]
PROFILE_PREFIX_ALLOWLIST = ("nutrition_defaults.",)
HOUSEHOLD_PROFILE_KEYS = (
    "nutrition_defaults.homemade_ingredient_quantity_basis",
    "nutrition_defaults.coffee",
    "nutrition_defaults.egg",
)
SECTION_NAME_ALLOWLIST = {"frontmatter", "formula", "yield"}


class EvidenceBundleError(RuntimeError):
    pass


def _loads_json(raw: str) -> Any:
    return json.loads(raw)


def _load_payload(raw: str) -> dict[str, Any]:
    payload = _loads_json(raw)
    if not isinstance(payload, dict):
        raise EvidenceBundleError("evidence bundle payload must be an object")
    return payload


def _path_value(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _resolve_db_path(payload: dict[str, Any]) -> Path:
    raw = _path_value(payload, "db", "db_path")
    path = default_db_path() if raw is None else Path(raw).expanduser()
    if not path.is_file():
        raise EvidenceBundleError(f"nutrition database does not exist: {path}")
    return path


def _resolve_profile_path(payload: dict[str, Any], *, profile_keys_requested: bool = False) -> Path | None:
    raw = _path_value(payload, "profile_path", "profile")
    if raw is not None:
        return Path(raw).expanduser()
    if not profile_keys_requested:
        return None
    try:
        return user_profile_path()
    except OSError:
        # Profile evidence is optional. Canonical-path resolution failures are
        # represented as missing coverage rather than crashing the packet.
        return None


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text))


def _evidence_ref(namespace: str, identity: str) -> str:
    safe = re.sub(r"[^a-z0-9._:-]+", "-", _normalize(identity).replace(" ", "-"))[:96].strip("-")
    if not safe:
        safe = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{namespace}:{safe}"


def _sanitize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _json_value(value)
        for key, value in spec.items()
        if key not in {"db", "db_path"}
    }


def _has_retrieval_query(spec: dict[str, Any]) -> bool:
    text = spec.get("text", spec.get("raw_text"))
    terms = spec.get("terms", spec.get("query_terms"))
    return bool(
        (isinstance(text, str) and text.strip())
        or (isinstance(terms, (list, tuple)) and any(isinstance(term, str) and term.strip() for term in terms))
    )


def _compact_retrieval(retrieval: dict[str, Any], *, limit: int = 3) -> dict[str, Any]:
    compact = {
        key: retrieval.get(key)
        for key in ("status", "schema_version", "suggested_action", "reason_codes", "read_only", "mode", "needs_clarification", "routing", "basis", "sources")
        if key in retrieval
    }
    candidates = retrieval.get("candidates")
    if isinstance(candidates, list):
        compact_candidates = []
        for candidate in candidates[:limit]:
            if not isinstance(candidate, dict):
                continue
            item = {
                key: candidate.get(key)
                for key in ("rank", "candidate_type", "label", "score", "match_type", "suggested_action", "provenance", "meal_time", "nutrients", "per_item_basis", "reason_codes")
                if key in candidate
            }
            provenance = candidate.get("provenance") or {}
            if provenance.get("table") == "nutrition_log" and provenance.get("entry_id") is not None:
                item["evidence_ref"] = f"nutrition_log:{provenance['entry_id']}"
            elif provenance.get("table") == "recipes" and provenance.get("recipe_id") is not None:
                item["evidence_ref"] = f"recipe:{provenance['recipe_id']}"
            compact_candidates.append(item)
        compact["candidates"] = compact_candidates
        compact["candidate_count"] = len(candidates)
        compact["candidates_truncated"] = len(candidates) > limit
    for key in ("historical_routine", "raw_vs_cooked_basis", "user_defaults", "warnings"):
        if key in retrieval:
            compact[key] = retrieval[key]
    return compact


def _validate_entry_requests(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EvidenceBundleError("entry_requests must be an array")
    requests: list[dict[str, Any]] = []
    for spec in raw:
        if not isinstance(spec, dict):
            raise EvidenceBundleError("entry request must be an object")
        entry_id = spec.get("entry_id")
        if isinstance(entry_id, bool) or not isinstance(entry_id, int) or entry_id <= 0:
            raise EvidenceBundleError("entry request entry_id must be a positive integer")
        terms = spec.get("item_terms", [])
        if isinstance(terms, str):
            terms = [terms]
        if not isinstance(terms, list) or any(not isinstance(term, str) or not term.strip() for term in terms):
            raise EvidenceBundleError("entry request item_terms must be non-empty strings")
        fields = spec.get("fields", DEFAULT_ENTRY_REQUEST_FIELDS)
        if not isinstance(fields, list) or any(field not in ENTRY_REQUEST_FIELD_ALLOWLIST for field in fields):
            raise EvidenceBundleError("entry request fields contain unsupported values")
        requests.append({"entry_id": entry_id, "item_terms": terms, "fields": list(fields)})
    return requests


def _item_phrases(item: dict[str, Any]) -> list[str]:
    values: list[Any] = [item.get("item"), item.get("name"), item.get("label"), item.get("brand")]
    aliases = item.get("aliases")
    values.extend(aliases if isinstance(aliases, list) else [aliases])
    return [_normalize(value) for value in values if _normalize(value)]


def _item_matches(item: dict[str, Any], terms: list[str]) -> bool:
    if not terms:
        return True
    phrases = _item_phrases(item)
    for term in terms:
        normalized = _normalize(term)
        tokens = normalized.split()
        if normalized and any(normalized == phrase or normalized in phrase for phrase in phrases):
            return True
        if tokens and any(all(token in phrase.split() for token in tokens) for phrase in phrases):
            return True
    return False


def _read_entry_requests(db: Path, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not requests:
        return []
    ids = list(dict.fromkeys(request["entry_id"] for request in requests))
    conn = duckdb.connect(str(db), read_only=True)
    try:
        placeholders = ", ".join("?" for _ in ids)
        cursor = conn.execute(f"SELECT * FROM nutrition_log WHERE entry_id IN ({placeholders})", ids)
        columns = [description[0] for description in cursor.description]
        rows = {int(row[0]): dict(zip(columns, row)) for row in cursor.fetchall()}
    finally:
        conn.close()

    results = []
    for request in requests:
        entry_id = request["entry_id"]
        row = rows.get(entry_id)
        result: dict[str, Any] = {
            "entry_id": entry_id,
            "item_terms": request["item_terms"],
            "requested_fields": request["fields"],
            "evidence_ref": f"nutrition_log:{entry_id}",
        }
        if row is None:
            result.update({"status": "missing", "items": [], "error": {"code": "missing_entry_id", "message": "explicit entry_id was not found"}})
            results.append(result)
            continue
        raw_items = row.get("food_items")
        if isinstance(raw_items, str):
            try:
                raw_items = json.loads(raw_items)
            except json.JSONDecodeError:
                raw_items = []
        items = raw_items if isinstance(raw_items, list) else []
        selected = []
        missing_items: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not _item_matches(item, request["item_terms"]):
                continue
            selected.append({
                "item_index": index,
                "evidence_ref": f"nutrition_log:{entry_id}:item:{index}",
                **{field: _json_value(item[field]) for field in request["fields"] if field in ENTRY_FIELD_ALLOWLIST and field in item},
            })
            absent = [field for field in request["fields"] if field in ENTRY_FIELD_ALLOWLIST and field not in item]
            if absent:
                missing_items.append({"item_index": index, "fields": absent})
        missing_provenance = [field for field in request["fields"] if field in ENTRY_PROVENANCE_FIELDS and row.get(field) is None]
        result["provenance"] = {
            "table": "nutrition_log", "entry_id": entry_id,
            **{field: _json_value(row[field]) for field in request["fields"] if field in ENTRY_PROVENANCE_FIELDS and row.get(field) is not None},
        }
        result["items"] = selected
        if not selected:
            result["status"] = "missing"
            result["error"] = {"code": "item_not_found", "message": "no requested item matched within the exact entry"}
        elif missing_items or missing_provenance:
            result["status"] = "partial"
            result["missing_fields"] = {"items": missing_items, "provenance": missing_provenance}
            result["error"] = {"code": "requested_fields_missing", "message": "one or more requested fields are absent from selected evidence"}
        else:
            result["status"] = "ok"
        results.append(result)
    return results


def _read_legacy_entries(db: Path, entry_ids: list[int]) -> list[dict[str, Any]]:
    """Preserve the baseline complete-row contract for queries[].entry_ids."""
    if not entry_ids:
        return []
    conn = duckdb.connect(str(db), read_only=True)
    try:
        placeholders = ", ".join("?" for _ in entry_ids)
        cursor = conn.execute(f"SELECT * FROM nutrition_log WHERE entry_id IN ({placeholders})", entry_ids)
        columns = [description[0] for description in cursor.description]
        by_id = {int(row[0]): dict(zip(columns, row)) for row in cursor.fetchall()}
    finally:
        conn.close()
    entries = []
    for entry_id in entry_ids:
        row = by_id.get(entry_id)
        if row is None:
            entries.append({"entry_id": entry_id, "found": False, "error": {"code": "missing_entry_id", "message": "explicit entry_id was not found"}})
            continue
        food_items = row.get("food_items")
        if isinstance(food_items, str):
            try:
                food_items = json.loads(food_items)
            except json.JSONDecodeError:
                pass
        entries.append({"entry_id": entry_id, "found": True, "entry": {**_json_value(row), "food_items": _json_value(food_items)}})
    return entries

def _read_profile_keys(profile_path: Path | None, keys: Any) -> list[dict[str, Any]]:
    if keys is None:
        return []
    if not isinstance(keys, list) or any(not isinstance(key, str) or not key.startswith(PROFILE_PREFIX_ALLOWLIST) for key in keys):
        raise EvidenceBundleError("profile_keys must be allowlisted nutrition_defaults.* YAML paths")
    data: Any = {}
    available = profile_path is not None and profile_path.is_file()
    if available:
        try:
            import yaml
            data = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        except Exception:
            available = False
            data = {}
    results = []
    for key in keys:
        value: Any = data
        found = available
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                found = False
                break
            value = value[part]
        results.append({
            "key": key,
            "status": "ok" if found else "missing",
            "value": _json_value(value) if found else None,
            "evidence_ref": _evidence_ref("profile", key),
        })
    return results


def _frontmatter_title(lines: list[str], fallback: str) -> str:
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            match = re.match(r"title:\s*[\"']?(.*?)[\"']?\s*$", line, re.I)
            if match and match.group(1).strip():
                return match.group(1).strip()
    for line in lines:
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _section_ranges(lines: list[str]) -> dict[str, tuple[int, int]]:
    ranges: dict[str, tuple[int, int]] = {}
    if lines and lines[0].strip() == "---":
        for index in range(1, min(len(lines), 100)):
            if lines[index].strip() == "---":
                ranges["frontmatter"] = (1, index + 1)
                break
    headings = []
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            headings.append((index, len(match.group(1)), _normalize(match.group(2))))
    for position, (start, level, title) in enumerate(headings):
        canonical = next((name for name in ("formula", "yield") if title == name or title.startswith(name + " ")), None)
        if canonical is None:
            continue
        end = len(lines)
        for next_start, next_level, _ in headings[position + 1:]:
            if next_level <= level:
                end = next_start
                break
        ranges[canonical] = (start + 1, end)
    return ranges


def _validate_kb_queries(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EvidenceBundleError("kb_recipe_queries must be an array")
    result = []
    for spec in raw:
        if not isinstance(spec, dict):
            raise EvidenceBundleError("KB recipe query must be an object")
        query = spec.get("query", spec.get("title", spec.get("slug")))
        sections = spec.get("sections", ["frontmatter", "formula", "yield"])
        if not isinstance(query, str) or not query.strip():
            raise EvidenceBundleError("KB recipe query requires title, slug, or query")
        if not isinstance(sections, list) or any(str(section).casefold() not in SECTION_NAME_ALLOWLIST for section in sections):
            raise EvidenceBundleError("KB recipe sections must be frontmatter, Formula, and/or Yield")
        result.append({"query": query.strip(), "sections": [str(section).casefold() for section in sections]})
    return result


def _kb_score(query: str, slug: str, title: str) -> tuple[int, float, str]:
    normalized = _normalize(query)
    candidates = [_normalize(slug.replace("-", " ")), _normalize(title)]
    if normalized in candidates:
        return (0, 1.0, slug)
    query_tokens = set(normalized.split())
    coverage = max((len(query_tokens.intersection(candidate.split())) / max(1, len(query_tokens)) for candidate in candidates), default=0.0)
    phrase = any(normalized and normalized in candidate for candidate in candidates)
    return (1 if phrase else 2, coverage, slug)


def _fit_packet_text(text: str, budget: dict[str, int]) -> tuple[str, bool]:
    """Consume one deterministic excerpt from the shared packet budget."""
    lines = text.splitlines()
    chosen: list[str] = []
    for line in lines:
        candidate = "\n".join([*chosen, line])
        if len(chosen) + 1 > budget["lines"] or len(candidate) > budget["chars"] or len(candidate.encode("utf-8")) > budget["bytes"]:
            break
        chosen.append(line)
    fitted = "\n".join(chosen)
    budget["lines"] -= len(chosen)
    budget["chars"] -= len(fitted)
    budget["bytes"] -= len(fitted.encode("utf-8"))
    return fitted, len(chosen) < len(lines)


def _read_kb_queries(requests: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not requests:
        return [], {"lines": 0, "chars": 0, "bytes": 0}
    root = KB_RECIPE_ROOT.resolve(strict=True)
    files = []
    for path in root.glob("*.md"):
        canonical = path.resolve(strict=True)
        if root not in canonical.parents:
            continue
        lines = canonical.read_text(encoding="utf-8").splitlines()
        files.append((canonical, lines, _frontmatter_title(lines, canonical.stem)))
    files.sort(key=lambda value: value[0].name)

    responses = []
    budget = {"lines": MAX_KB_PACKET_LINES, "chars": MAX_KB_PACKET_CHARS, "bytes": MAX_KB_PACKET_BYTES}
    for request in requests:
        ranked = sorted(
            ((_kb_score(request["query"], path.stem, title), path, lines, title) for path, lines, title in files),
            key=lambda value: (value[0][0], -value[0][1], value[0][2]),
        )
        matches = [value for value in ranked if value[0][0] < 2 or value[0][1] >= 0.5][:MAX_KB_MATCHES]
        recipes = []
        union_returned_sections: set[str] = set()
        complete_candidates = 0
        for _, path, lines, title in matches:
            ranges = _section_ranges(lines)
            sections = []
            candidate_returned: list[str] = []
            candidate_truncated: list[str] = []
            for section_name in request["sections"]:
                if section_name not in ranges:
                    continue
                start, end = ranges[section_name]
                text, truncated = _fit_packet_text("\n".join(lines[start:end]), budget)
                consumed = text.count("\n") + (1 if text else 0)
                if not text:
                    truncated = True
                if truncated:
                    candidate_truncated.append(section_name)
                else:
                    candidate_returned.append(section_name)
                    union_returned_sections.add(section_name)
                sections.append({
                    "name": section_name, "text": text, "line_start": start + 1,
                    "line_end": start + consumed, "evidence_ref": f"kb-recipe:{path.stem}#{section_name}",
                    "truncated": truncated,
                })
            candidate_missing = [name for name in request["sections"] if name not in candidate_returned]
            candidate_status = "ok" if not candidate_missing else ("partial" if sections else "missing")
            if candidate_status == "ok":
                complete_candidates += 1
            recipes.append({
                "slug": path.stem, "title": title, "canonical_path": str(path),
                "evidence_ref": f"kb-recipe:{path.stem}",
                "requested_sections": list(request["sections"]),
                "returned_sections": candidate_returned,
                "missing_sections": candidate_missing,
                "truncated_sections": candidate_truncated,
                "status": candidate_status,
                "sections": sections,
            })
        # Query success is candidate-local: sections from different recipes may
        # never be unioned to manufacture a complete recipe.
        missing_sections = [] if complete_candidates else list(request["sections"])
        status = "missing" if not recipes else ("ok" if complete_candidates else "partial")
        response: dict[str, Any] = {
            "query": request["query"], "requested_sections": request["sections"],
            "returned_sections": sorted(union_returned_sections), "missing_sections": missing_sections,
            "complete_candidate_count": complete_candidates,
            "status": status, "recipes": recipes,
        }
        if status != "ok":
            response["error"] = {
                "code": "kb_sections_missing" if recipes else "kb_recipe_not_found",
                "message": "no individual KB recipe candidate returned every requested section",
                "truncated_sections": sorted({name for recipe in recipes for name in recipe["truncated_sections"]}),
            }
        responses.append(response)
    return responses, {
        "lines": MAX_KB_PACKET_LINES - budget["lines"],
        "chars": MAX_KB_PACKET_CHARS - budget["chars"],
        "bytes": MAX_KB_PACKET_BYTES - budget["bytes"],
    }

def _legacy_entry_ids(spec: dict[str, Any]) -> list[int]:
    raw = spec.get("entry_ids", spec.get("entry_id"))
    if raw is None:
        return []
    values = [raw] if isinstance(raw, int) and not isinstance(raw, bool) else raw
    if not isinstance(values, (list, tuple)) or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        raise EvidenceBundleError("entry_ids must contain only positive integers")
    return list(values)


def nutrition_evidence_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise EvidenceBundleError("evidence bundle payload must be an object")
    compact_output = payload.get("compact", True)
    if not isinstance(compact_output, bool):
        raise EvidenceBundleError("compact must be a boolean")
    db = _resolve_db_path(payload)
    queries = payload.get("queries", [])
    if not isinstance(queries, list):
        raise EvidenceBundleError("queries must be an array")
    entry_requests = _validate_entry_requests(payload.get("entry_requests"))
    profile_keys = payload.get("profile_keys")
    include_household = payload.get("include_household_defaults", False)
    if not isinstance(include_household, bool):
        raise EvidenceBundleError("include_household_defaults must be a boolean")
    if include_household:
        if profile_keys is not None and (
            not isinstance(profile_keys, list)
            or any(not isinstance(key, str) or not key.startswith(PROFILE_PREFIX_ALLOWLIST) for key in profile_keys)
        ):
            raise EvidenceBundleError("profile_keys must be allowlisted nutrition_defaults.* YAML paths")
        profile_keys = list(dict.fromkeys([*HOUSEHOLD_PROFILE_KEYS, *(profile_keys or [])]))
    profile_path = _resolve_profile_path(payload, profile_keys_requested=bool(profile_keys))
    kb_requests = _validate_kb_queries(payload.get("kb_recipe_queries"))
    if not queries and not entry_requests and not profile_keys and not kb_requests:
        raise EvidenceBundleError("at least one requested local evidence class is required")

    results: list[dict[str, Any]] = []
    failures = 0
    for index, spec in enumerate(queries):
        result: dict[str, Any] = {"index": index}
        if not isinstance(spec, dict):
            result.update({"status": "error", "error": {"code": "invalid_query", "message": "query spec must be an object"}})
            failures += 1
            results.append(result)
            continue
        entry_ids = _legacy_entry_ids(spec)
        if not _has_retrieval_query(spec) and not entry_ids:
            result.update({"status": "error", "error": {"code": "invalid_query", "message": "query spec must include retrieval text/terms or explicit entry_ids"}})
            failures += 1
            results.append(result)
            continue
        result["query"] = _sanitize_spec(spec)
        if entry_ids:
            # Deliberately baseline-shaped: no evidence refs, item indexes, field
            # filtering, or transformed provenance in this legacy surface.
            result["entries"] = _read_legacy_entries(db, entry_ids)
        if _has_retrieval_query(spec):
            try:
                retrieval = retrieve_nutrition(db, spec, profile_path=profile_path)
                result["retrieval"] = _compact_retrieval(retrieval) if compact_output else retrieval
                result["status"] = "ok" if retrieval.get("candidates") else "missing"
            except RetrievalError as exc:
                result.update({"status": "error", "retrieval_error": {"code": exc.code, "message": exc.message}})
                failures += 1
            except Exception:
                result.update({"status": "error", "retrieval_error": {"code": "retrieval_failed", "message": "nutrition retrieval failed safely"}})
                failures += 1
        else:
            # Baseline treats missing IDs as row-level results, not query failure.
            result["status"] = "ok"
        results.append(result)

    entries = _read_entry_requests(db, entry_requests)
    selected_profile = _read_profile_keys(profile_path, profile_keys)
    try:
        kb_recipes, kb_budget_used = _read_kb_queries(kb_requests)
    except (OSError, UnicodeError):
        kb_recipes = [{
            "query": request["query"], "requested_sections": request["sections"],
            "returned_sections": [], "missing_sections": request["sections"], "status": "error", "recipes": [],
            "error": {"code": "kb_unavailable", "message": "canonical KB recipe source is unavailable"},
        } for request in kb_requests]
        kb_budget_used = {"lines": 0, "chars": 0, "bytes": 0}

    requested_classes: list[str] = []
    returned_classes: list[str] = []
    missing_classes: list[str] = []
    errors: list[dict[str, Any]] = []
    request_statuses: dict[str, list[str]] = {}
    sections = (("queries", queries, results), ("entries", entry_requests, entries), ("profile", profile_keys or [], selected_profile), ("kb_recipes", kb_requests, kb_recipes))
    for name, requested, returned in sections:
        if not requested:
            continue
        requested_classes.append(name)
        statuses = [str(item.get("status")) for item in returned]
        request_statuses[name] = statuses
        if any(status in {"ok", "partial"} for status in statuses):
            returned_classes.append(name)
        if not returned or any(status != "ok" for status in statuses):
            missing_classes.append(name)
        for request_index, item in enumerate(returned):
            error = item.get("error") or item.get("retrieval_error")
            if error:
                errors.append({"class": name, "request_index": request_index, **error})
    status = "ok" if not missing_classes else "partial"
    return {
        "status": status, "read_only": True, "network_free": True,
        "db_path": str(db), "profile_path": str(profile_path) if profile_path is not None else None,
        "compact": compact_output, "query_count": len(results), "failure_count": failures,
        "results": results, "entries": entries, "profile": selected_profile, "kb_recipes": kb_recipes,
        "kb_packet_budget": {
            "limits": {"lines": MAX_KB_PACKET_LINES, "chars": MAX_KB_PACKET_CHARS, "bytes": MAX_KB_PACKET_BYTES},
            "used": kb_budget_used,
        },
        "coverage": {
            "scope": "requested_local_evidence", "requested_classes": requested_classes,
            "returned_classes": returned_classes, "missing_classes": missing_classes,
            "request_statuses": request_statuses, "errors": errors,
        },
    }

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch read-only nutrition evidence retrieval")
    parser.add_argument("--json", required=True, help="Structured JSON bundle, or '-' for stdin")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        raw = sys.stdin.read() if args.json == "-" else args.json
        result = nutrition_evidence_bundle(_load_payload(raw))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2 if args.pretty else None, allow_nan=False))
        return 0
    except json.JSONDecodeError as exc:
        error = {"status": "error", "error": {"code": "invalid_json", "message": str(exc)}}
    except (ValueError, EvidenceBundleError) as exc:
        error = {"status": "error", "error": {"code": "invalid_request", "message": str(exc)}}
    except RetrievalError as exc:
        error = {"status": "error", "error": {"code": exc.code, "message": exc.message}}
    except Exception:
        error = {"status": "error", "error": {"code": "bundle_failed", "message": "nutrition evidence bundle failed safely"}}
    print(json.dumps(error, ensure_ascii=False, sort_keys=True, allow_nan=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
