from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "log-nutrition" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from nutrition_ingest import migrate_database  # noqa: E402
import daily_nutrition_summary as dns  # noqa: E402


SUMMARY_FIELDS = ["calories", "protein_g", "carbs_g", "fat_total_g", "fat_saturated_g", "fiber_g"]


def _seed_day(db: Path, meal_rows: list[dict[str, object]]) -> None:
    migrate_database(db)
    conn = duckdb.connect(str(db))
    try:
        for row in meal_rows:
            fields = ["entry_id", "meal_time", "meal_type", "meal_name", *SUMMARY_FIELDS, "food_items", "source"]
            values = [
                row["entry_id"],
                row["meal_time"],
                row.get("meal_type"),
                row.get("meal_name"),
                *(row.get(field) for field in SUMMARY_FIELDS),
                json.dumps(row.get("food_items", [])),
                row.get("source", "test"),
            ]
            conn.execute(
                f"INSERT INTO nutrition_log ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                values,
            )
    finally:
        conn.close()


def test_complete_day_sums_meals_and_is_complete(tmp_path):
    db = tmp_path / "complete.duckdb"
    _seed_day(
        db,
        [
            {
                "entry_id": 1,
                "meal_time": "2026-08-19 08:00:00",
                "meal_type": "breakfast",
                "meal_name": "eggs",
                "calories": 200.0,
                "protein_g": 14.0,
                "carbs_g": 2.0,
                "fat_total_g": 15.0,
                "fat_saturated_g": 4.0,
                "fiber_g": 0.0,
            },
            {
                "entry_id": 2,
                "meal_time": "2026-08-19 12:30:00",
                "meal_type": "lunch",
                "meal_name": "salad",
                "calories": 300.0,
                "protein_g": 20.0,
                "carbs_g": 30.0,
                "fat_total_g": 10.0,
                "fat_saturated_g": 2.0,
                "fiber_g": 8.0,
            },
        ],
    )

    summary = dns.daily_nutrition_summary("2026-08-19", db=str(db))

    assert summary["meal_count"] == 2
    assert [meal["meal_time"] for meal in summary["meals"]] == ["2026-08-19 08:00:00", "2026-08-19 12:30:00"]
    assert summary["daily_totals"]["calories"]["known_sum"] == 500.0
    assert summary["daily_totals"]["protein_g"]["known_sum"] == 34.0
    assert summary["daily_totals"]["fat_saturated_g"] == {
        "known_sum": 6.0,
        "known_count": 2,
        "missing_count": 0,
        "meal_count": 2,
        "complete": True,
    }
    human = dns.render_human(summary)
    assert "No meals logged" not in human
    assert "complete" in human


def test_partial_saturated_fat_coverage_exposes_missing_count_and_incomplete(tmp_path):
    db = tmp_path / "partial.duckdb"
    _seed_day(
        db,
        [
            {
                "entry_id": 1,
                "meal_time": "2026-08-19 08:00:00",
                "meal_type": "breakfast",
                "meal_name": "yogurt",
                "calories": 120.0,
                "protein_g": 10.0,
                "carbs_g": 12.0,
                "fat_total_g": 3.0,
                "fat_saturated_g": 1.5,
                "fiber_g": 0.0,
            },
            {
                "entry_id": 2,
                "meal_time": "2026-08-19 13:00:00",
                "meal_type": "lunch",
                "meal_name": "soup",
                "calories": 250.0,
                "protein_g": 18.0,
                "carbs_g": 20.0,
                "fat_total_g": 8.0,
                "fat_saturated_g": None,
                "fiber_g": 4.0,
            },
        ],
    )

    summary = dns.daily_nutrition_summary("2026-08-19", db=str(db))

    sat = summary["daily_totals"]["fat_saturated_g"]
    assert sat["known_sum"] == 1.5
    assert sat["known_count"] == 1
    assert sat["missing_count"] == 1
    assert sat["complete"] is False
    assert "incomplete" in dns.render_human(summary)
    assert json.dumps(summary)[-1] == "}"


def test_empty_day_returns_empty_meals_and_zero_coverage(tmp_path):
    db = tmp_path / "empty.duckdb"
    migrate_database(db)

    summary = dns.daily_nutrition_summary("2026-08-19", db=str(db))

    assert summary["meal_count"] == 0
    assert summary["meals"] == []
    for field in SUMMARY_FIELDS:
        assert summary["daily_totals"][field] == {
            "known_sum": 0.0,
            "known_count": 0,
            "missing_count": 0,
            "meal_count": 0,
            "complete": True,
        }
    assert "No meals logged" in dns.render_human(summary)


def test_uses_read_only_connection_and_does_not_mutate(tmp_path, monkeypatch):
    db = tmp_path / "readonly.duckdb"
    _seed_day(
        db,
        [
            {
                "entry_id": 1,
                "meal_time": "2026-08-19 08:00:00",
                "meal_type": "breakfast",
                "meal_name": "egg",
                "calories": 78.0,
                "protein_g": 6.3,
                "carbs_g": 0.6,
                "fat_total_g": 5.3,
                "fat_saturated_g": 1.6,
                "fiber_g": 0.0,
            }
        ],
    )
    orig_connect = duckdb.connect
    before = orig_connect(str(db), read_only=True).execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0]
    seen_read_only: list[bool | None] = []

    def spy_connect(*args, **kwargs):
        seen_read_only.append(kwargs.get("read_only"))
        return orig_connect(*args, **kwargs)

    monkeypatch.setattr(dns.duckdb, "connect", spy_connect)

    summary = dns.daily_nutrition_summary("2026-08-19", db=str(db))

    after = orig_connect(str(db), read_only=True).execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0]
    assert summary["meal_count"] == 1
    assert seen_read_only == [True]
    assert before == after == 1
