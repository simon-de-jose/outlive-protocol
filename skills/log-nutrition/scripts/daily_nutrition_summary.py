#!/usr/bin/env python3
"""Read-only daily nutrition summary for the Food Journal post-log utility."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import duckdb

from bootstrap.env import db_path as default_db_path

SUMMARY_FIELDS = ["calories", "protein_g", "carbs_g", "fat_total_g", "fat_saturated_g", "fiber_g"]
MEAL_FIELDS = ["entry_id", "meal_time", "meal_type", "meal_name", *SUMMARY_FIELDS]


def _resolve_db_path(explicit_db: str | None) -> Path:
    path = Path(explicit_db).expanduser() if explicit_db else default_db_path()
    if not path.is_file():
        raise FileNotFoundError(f"nutrition database does not exist: {path}")
    return path


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("--date must be in YYYY-MM-DD format") from exc


def _coerce_meal_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def _coerce_number(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _summarize_rows(rows: list[dict[str, Any]], summary_date: date, db: Path) -> dict[str, Any]:
    meal_count = len(rows)
    meals: list[dict[str, Any]] = []
    totals: dict[str, dict[str, Any]] = {}
    for field in SUMMARY_FIELDS:
        totals[field] = {"known_sum": 0.0, "known_count": 0, "missing_count": 0, "meal_count": meal_count, "complete": True}
    for row in rows:
        meal = {
            "entry_id": row["entry_id"],
            "meal_time": _coerce_meal_time(row["meal_time"]),
            "meal_type": row.get("meal_type"),
            "meal_name": row.get("meal_name"),
        }
        for field in SUMMARY_FIELDS:
            value = _coerce_number(row.get(field))
            meal[field] = value
            stats = totals[field]
            if value is None:
                stats["missing_count"] += 1
                stats["complete"] = False
            else:
                stats["known_sum"] += value
                stats["known_count"] += 1
        meals.append(meal)
    for field in SUMMARY_FIELDS:
        totals[field]["known_sum"] = round(totals[field]["known_sum"], 6)
        totals[field]["complete"] = totals[field]["missing_count"] == 0
    return {
        "date": summary_date.isoformat(),
        "db_path": str(db),
        "meal_count": meal_count,
        "meals": meals,
        "daily_totals": totals,
    }


def daily_nutrition_summary(summary_date: str, *, db: str | None = None) -> dict[str, Any]:
    day = _parse_date(summary_date)
    db_file = _resolve_db_path(db)
    next_day = day + timedelta(days=1)
    query = (
        "SELECT " + ", ".join(MEAL_FIELDS) +
        " FROM nutrition_log WHERE meal_time >= ? AND meal_time < ? ORDER BY meal_time, entry_id"
    )
    conn = duckdb.connect(str(db_file), read_only=True)
    try:
        rows = conn.execute(query, [day.isoformat(), next_day.isoformat()]).fetchall()
        columns = [desc[0] for desc in conn.description]
    finally:
        conn.close()
    row_dicts = [dict(zip(columns, row)) for row in rows]
    return _summarize_rows(row_dicts, day, db_file)


def _format_value(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def render_human(summary: dict[str, Any]) -> str:
    lines = [f"Nutrition summary for {summary['date']} ({summary['meal_count']} meals)"]
    if summary["meal_count"] == 0:
        lines.append("No meals logged.")
    else:
        lines.append("Meals:")
        for meal in summary["meals"]:
            label = meal.get("meal_name") or meal.get("meal_type") or "meal"
            lines.append(
                f"- {meal['meal_time']} | {label} | "
                f"calories={_format_value(meal['calories'])} protein_g={_format_value(meal['protein_g'])} "
                f"carbs_g={_format_value(meal['carbs_g'])} fat_total_g={_format_value(meal['fat_total_g'])} "
                f"fat_saturated_g={_format_value(meal['fat_saturated_g'])} fiber_g={_format_value(meal['fiber_g'])}"
            )
    lines.append("Daily totals:")
    for field in SUMMARY_FIELDS:
        stats = summary["daily_totals"][field]
        status = "complete" if stats["complete"] else "incomplete"
        lines.append(
            f"- {field}: {_format_value(stats['known_sum'])} ({stats['known_count']}/{stats['meal_count']} meals, {status})"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only daily nutrition summary")
    parser.add_argument("--date", required=True, help="Summary date in YYYY-MM-DD format")
    parser.add_argument("--db", help="Database path; defaults to bootstrap.env db_path()")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text")
    args = parser.parse_args()

    try:
        summary = daily_nutrition_summary(args.date, db=args.db)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(render_human(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
