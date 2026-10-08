#!/usr/bin/env python3
"""Nutrition ingest wrapper that returns the write result and same-day summary."""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bootstrap.env import db_path as default_db_path
from daily_nutrition_summary import daily_nutrition_summary
from nutrition_ingest import identity_from_payload, ingest_nutrition, ingest_nutrition_many


class WriteSummaryError(RuntimeError):
    pass


def _loads_json(raw: str) -> Any:
    return json.loads(raw)


def _load_payload(raw: str) -> dict[str, Any]:
    payload = _loads_json(raw)
    if not isinstance(payload, dict):
        raise WriteSummaryError("nutrition payload must be an object")
    return payload


def _meal_date(meal_time: Any) -> str:
    if isinstance(meal_time, datetime):
        return meal_time.date().isoformat()
    if not isinstance(meal_time, str) or not meal_time.strip():
        raise WriteSummaryError("meal_time is required to compute the daily summary")
    value = meal_time.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError as exc:
        raise WriteSummaryError("meal_time must be an ISO timestamp with a time component") from exc


def _entries_from_payload(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    if "db" in payload or "db_path" in payload or "profile_path" in payload:
        raise WriteSummaryError("write payload must not include unsupported db/db_path/profile_path keys; use environment or CLI options")
    if "entries" not in payload:
        return [payload], False
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise WriteSummaryError("entries must be a non-empty list")
    inherited = {key: payload[key] for key in ("provider", "message_id", "discord_message_id") if key in payload}
    expanded: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise WriteSummaryError("each entry must be an object")
        merged = {**inherited, **entry}
        if "event_key" not in merged:
            raise WriteSummaryError("multi-entry writes require an explicit event_key for every entry")
        expanded.append(merged)
    return expanded, True


def log_nutrition_with_summary(payload: dict[str, Any], *, allow_anonymous: bool = False, db: str | Path | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise WriteSummaryError("nutrition payload must be an object")

    db_path = Path(db).expanduser() if db is not None else default_db_path()
    entries, multi = _entries_from_payload(payload)
    if not multi:
        identity = identity_from_payload(entries[0])
        if identity is None and not allow_anonymous:
            raise WriteSummaryError(
                "nutrition writes require provider and message_id; pass --allow-anonymous only for non-idempotent imports",
            )
        if identity is None:
            warnings.warn("anonymous nutrition write is non-idempotent", RuntimeWarning, stacklevel=2)
        write_result = ingest_nutrition(db_path, entries[0], identity=identity)
        committed_entries = [write_result["result"]["entry"]]
    else:
        if allow_anonymous:
            raise WriteSummaryError("multi-entry nutrition writes are always identified; remove --allow-anonymous")
        write_result = ingest_nutrition_many(db_path, entries)
        committed_entries = [item["result"]["entry"] for item in write_result["results"]]

    dates = {_meal_date(entry.get("meal_time")) for entry in committed_entries}
    if len(dates) != 1:
        raise WriteSummaryError("multi-entry write+summary currently requires all entries on one local date")
    summary_date = next(iter(dates))
    try:
        summary = daily_nutrition_summary(summary_date, db=str(db_path))
    except Exception as exc:
        return {
            "status": "summary_unavailable",
            "commit_status": "committed",
            "db_path": str(db_path),
            "meal_date": summary_date,
            "write_result": write_result,
            "daily_summary": None,
            "summary_error": {
                "code": "summary_unavailable",
                "message": "daily summary failed after commit",
                "detail": str(exc),
            },
        }
    return {
        "status": "ok",
        "commit_status": "committed",
        "db_path": str(db_path),
        "meal_date": summary_date,
        "write_result": write_result,
        "daily_summary": summary,
        "summary_error": None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Log nutrition and return the same-day summary")
    parser.add_argument("--json", required=True, help="JSON data for the entry, or '-' for stdin")
    parser.add_argument(
        "--allow-anonymous",
        action="store_true",
        help="allow a non-idempotent import without provider/message_id (warning emitted)",
    )
    parser.add_argument("--db", help="database path for tests/rehearsals; omit in production to use bootstrap.env")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        raw = sys.stdin.read() if args.json == "-" else args.json
        result = log_nutrition_with_summary(_load_payload(raw), allow_anonymous=args.allow_anonymous, db=args.db)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2 if args.pretty else None, allow_nan=False))
        return 0
    except (json.JSONDecodeError, ValueError, WriteSummaryError) as exc:
        error = {"status": "error", "error": {"code": "invalid_json", "message": str(exc)}}
    except Exception:
        error = {"status": "error", "error": {"code": "write_failed", "message": "nutrition write and summary failed safely"}}
    print(json.dumps(error, ensure_ascii=False, sort_keys=True, allow_nan=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
