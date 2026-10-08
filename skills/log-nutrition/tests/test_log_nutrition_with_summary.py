from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "skills" / "log-nutrition" / "scripts"
WRAPPER = SCRIPTS_DIR / "log_nutrition_with_summary.py"

sys.path.insert(0, str(SCRIPTS_DIR))

from nutrition_ingest import migrate_database  # noqa: E402
import log_nutrition_with_summary as lnws  # noqa: E402


def _payload(message_id: str = "meal-1") -> dict[str, object]:
    return {
        "meal_time": "2026-08-19T08:05:00",
        "meal_type": "breakfast",
        "meal_name": "egg breakfast",
        "meal_description": "wrapper integration test",
        "provider": "discord",
        "message_id": message_id,
        "food_items": [
            {
                "item": "egg",
                "portion_g": 50,
                "calories": 78,
                "protein_g": 6.3,
                "carbs_g": 0.6,
                "fat_total_g": 5.3,
                "fiber_g": 0.0,
            }
        ],
        "calories": 78,
        "protein_g": 6.3,
        "carbs_g": 0.6,
        "fat_total_g": 5.3,
        "fiber_g": 0.0,
        "source": "test",
    }


def _run(payload: dict[str, object], *, env: dict[str, str]) -> dict[str, object]:
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), "--json", json.dumps(payload)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert not proc.stderr.strip(), proc.stderr
    return json.loads(proc.stdout)


def test_write_summary_replay_and_missing_saturated_fat_coverage(tmp_path):
    db = tmp_path / "wrapper.duckdb"
    migrate_database(db)
    env = {**os.environ, "HEALTH_DB_PATH": str(db)}

    first = _run(_payload(), env=env)
    assert first["status"] == "ok"
    assert first["meal_date"] == "2026-08-19"
    assert first["write_result"]["replayed"] is False
    assert first["daily_summary"]["meal_count"] == 1
    sat = first["daily_summary"]["daily_totals"]["fat_saturated_g"]
    assert sat["missing_count"] == 1
    assert sat["complete"] is False

    second = _run(_payload(), env=env)
    assert second["write_result"]["replayed"] is True
    assert second["write_result"]["result"] == first["write_result"]["result"]
    assert second["daily_summary"]["meal_count"] == 1

    conn = duckdb.connect(str(db), read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM nutrition_ingest_receipts").fetchone()[0] == 1
    finally:
        conn.close()


def test_summary_failure_after_commit_returns_committed_write_and_replays_safely(monkeypatch, tmp_path):
    db = tmp_path / "summary-failure.duckdb"
    migrate_database(db)
    payload = _payload("summary-failure-1")
    monkeypatch.setenv("HEALTH_DB_PATH", str(db))

    def boom(*args, **kwargs):
        raise RuntimeError("summary exploded")

    monkeypatch.setattr(lnws, "daily_nutrition_summary", boom)

    first = lnws.log_nutrition_with_summary(payload)
    assert first["status"] == "summary_unavailable"
    assert first["commit_status"] == "committed"
    assert first["daily_summary"] is None
    assert first["summary_error"]["code"] == "summary_unavailable"
    assert first["write_result"]["replayed"] is False
    assert first["write_result"]["result"]["entry"]["meal_name"] == "egg breakfast"

    second = lnws.log_nutrition_with_summary(payload)
    assert second["status"] == "summary_unavailable"
    assert second["commit_status"] == "committed"
    assert second["write_result"]["replayed"] is True
    assert second["write_result"]["result"] == first["write_result"]["result"]

    conn = duckdb.connect(str(db), read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM nutrition_ingest_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM nutrition_ingest_identities").fetchone()[0] == 1
    finally:
        conn.close()


def test_multi_entry_one_source_message_is_atomic_idempotent_and_literal(tmp_path):
    db = tmp_path / "multi.duckdb"
    migrate_database(db)
    payload = {
        "provider": "discord",
        "message_id": "literal-source-1",
        "entries": [
            {**_payload("ignored-a"), "event_key": "breakfast-0805", "meal_name": "eggs", "meal_time": "2026-08-19T08:05:00"},
            {**_payload("ignored-b"), "event_key": "snack-1030", "meal_name": "fruit", "meal_time": "2026-08-19T10:30:00", "calories": 50},
        ],
    }
    for entry in payload["entries"]:
        entry.pop("message_id", None)
        entry.pop("provider", None)
    result = lnws.log_nutrition_with_summary(payload, db=db)
    assert result["status"] == "ok"
    assert len(result["write_result"]["results"]) == 2
    replay = lnws.log_nutrition_with_summary(payload, db=db)
    assert replay["write_result"]["replayed"] is True
    assert replay["daily_summary"]["meal_count"] == 2
    conn = duckdb.connect(str(db), read_only=True)
    try:
        rows = conn.execute("SELECT ingest_provider,ingest_message_id,ingest_event_key FROM nutrition_log ORDER BY entry_id").fetchall()
        assert rows == [("discord", "literal-source-1", "breakfast-0805"), ("discord", "literal-source-1", "snack-1030")]
        assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0] == 2
    finally:
        conn.close()


def test_staged_day_completion_preserves_original_identity_and_totals(tmp_path):
    db = tmp_path / "staged.duckdb"
    migrate_database(db)
    breakfast = {**_payload(), "event_key": "breakfast"}
    dinner = {**_payload(), "event_key": "dinner", "meal_type": "dinner",
              "meal_time": "2026-09-23T18:30:00-07:00", "calories": 200,
              "source": "estimate; clarification message:followup"}
    breakfast["meal_time"] = "2026-09-23T09:00:00-07:00"
    for entry in (breakfast, dinner):
        entry.pop("provider")
        entry.pop("message_id")
    source = {"provider": "discord", "message_id": "original-source"}
    first = lnws.log_nutrition_with_summary({**source, "entries": [breakfast]}, db=db)
    assert first["commit_status"] == "committed"
    completed = lnws.log_nutrition_with_summary({**source, "entries": [dinner]}, db=db)
    assert completed["daily_summary"]["meal_count"] == 2
    replay = lnws.log_nutrition_with_summary({**source, "entries": [dinner]}, db=db)
    assert replay["write_result"]["replayed"] is True
    with duckdb.connect(str(db), read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*), SUM(calories) FROM nutrition_log").fetchone() == (2, 278)
        assert conn.execute("SELECT DISTINCT ingest_message_id FROM nutrition_log").fetchall() == [("original-source",)]


def test_multi_entry_write_failure_rolls_back_all_entries(tmp_path):
    db = tmp_path / "multi-rollback.duckdb"
    migrate_database(db)
    payload = {
        "provider": "discord",
        "message_id": "literal-source-rollback",
        "entries": [
            {**_payload("a"), "event_key": "first", "meal_time": "2026-08-19T08:05:00"},
            {**_payload("b"), "event_key": "bad key with spaces", "meal_time": "2026-08-19T09:05:00"},
        ],
    }
    for entry in payload["entries"]:
        entry.pop("message_id", None)
        entry.pop("provider", None)
    try:
        lnws.log_nutrition_with_summary(payload, db=db)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid event_key should fail")
    conn = duckdb.connect(str(db), read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM nutrition_ingest_receipts").fetchone()[0] == 0
    finally:
        conn.close()
