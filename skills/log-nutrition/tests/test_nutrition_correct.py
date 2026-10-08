"""Focused tests for the narrow nutrition correction path."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "skills" / "log-nutrition" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from nutrition_correct import nutrition_correct  # noqa: E402
from nutrition_ingest import (  # noqa: E402
    ReceiptIntegrityError,
    _digest,
    correct_nutrition,
    ingest_nutrition,
    migrate_database,
    replay_result,
)


def _meal(name: str, *, message_id: str, provider: str = "discord") -> dict[str, object]:
    return {
        "meal_time": "2026-08-19T08:00:00",
        "meal_type": "breakfast",
        "meal_name": name,
        "meal_description": "fixture",
        "food_items": [{"item": "egg", "portion_g": 50, "calories": 78}],
        "calories": 78,
        "protein_g": 6.3,
        "carbs_g": 0.6,
        "fat_total_g": 5.3,
        "source": "fixture",
        "provider": provider,
        "message_id": message_id,
    }


def _receipt_state(db: Path, identity: tuple[str, str]):
    conn = duckdb.connect(str(db), read_only=True)
    row = conn.execute(
        "SELECT entry_id, meal_name, calories, ingest_provider, ingest_message_id, ingest_event_key FROM nutrition_log WHERE ingest_provider=? AND ingest_message_id=? AND ingest_event_key=?",
        [*identity, "default"],
    ).fetchone()
    receipt = conn.execute(
        "SELECT entry_id, result_json, integrity_digest, committed_at FROM nutrition_ingest_receipts WHERE provider=? AND message_id=? AND event_key=?",
        [*identity, "default"],
    ).fetchone()
    conn.close()
    return row, receipt


def test_correction_replaces_row_and_preserves_replay(monkeypatch, tmp_path):
    db = tmp_path / "correct.duckdb"
    migrate_database(db)
    identity = ("discord", "corr-1")
    ingest_nutrition(db, _meal("original", message_id=identity[1]), identity=identity)
    monkeypatch.setenv("HEALTH_DB_PATH", str(db))

    corrected = {
        **_meal("corrected bowl", message_id=identity[1]),
        "meal_description": "corrected fixture",
        "food_items": [{"item": "tofu", "portion_g": 100, "calories": 144}],
        "calories": 144,
        "protein_g": 15.7,
        "carbs_g": 3.8,
        "fat_total_g": 8.7,
        "notes": "corrected",
    }

    result = nutrition_correct(corrected)
    assert result["entry"]["entry_id"] == 1
    assert result["entry"]["meal_name"] == "corrected bowl"

    row, receipt = _receipt_state(db, identity)
    assert row == (1, "corrected bowl", 144.0, "discord", "corr-1", "default")
    assert receipt is not None
    envelope = json.loads(receipt[1])
    assert envelope["result"] == result

    conn = duckdb.connect(str(db), read_only=True)
    committed_at = conn.execute(
        "SELECT committed_at FROM nutrition_ingest_receipts WHERE provider=? AND message_id=? AND event_key=?",
        [*identity, "default"],
    ).fetchone()[0]
    canonical_row = conn.execute(
        "SELECT entry_id,meal_time,meal_type,meal_name,meal_description,food_items,calories,protein_g,carbs_g,fat_total_g,fat_saturated_g,fat_unsaturated_g,fat_trans_g,fiber_g,sugar_g,sodium_mg,potassium_mg,calcium_mg,iron_mg,magnesium_mg,vitamin_d_mcg,vitamin_b12_mcg,vitamin_c_mg,cholesterol_mg,source,logged_at,notes,ingest_provider,ingest_message_id,ingest_event_key FROM nutrition_log WHERE entry_id=1",
    ).fetchone()
    conn.close()
    fields = [
        "entry_id", "meal_time", "meal_type", "meal_name", "meal_description", "food_items",
        "calories", "protein_g", "carbs_g", "fat_total_g", "fat_saturated_g", "fat_unsaturated_g",
        "fat_trans_g", "fiber_g", "sugar_g", "sodium_mg", "potassium_mg", "calcium_mg", "iron_mg",
        "magnesium_mg", "vitamin_d_mcg", "vitamin_b12_mcg", "vitamin_c_mg", "cholesterol_mg", "source",
        "logged_at", "notes", "ingest_provider", "ingest_message_id", "ingest_event_key",
    ]
    row_dict = dict(zip(fields, canonical_row))
    assert receipt[2] == _digest(row_dict, envelope, identity, 1, committed_at)
    assert replay_result(db, identity) == result
    replay = ingest_nutrition(db, {"meal_time": "not-a-time", "provider": identity[0], "message_id": identity[1]}, identity=identity)
    assert replay["replayed"] is True and replay["result"] == result


@pytest.mark.parametrize(
    "payload, identity, expected_exc, match",
    [
        ({"meal_time": "2026-08-19T08:00:00"}, None, ValueError, "require provider and message_id"),
        ({**_meal("wrong payload", message_id="wrong-2"), "provider": "discord", "message_id": "wrong-2"}, ("discord", "corr-2"), ValueError, "conflicts with payload identity"),
        ({**_meal("missing row", message_id="absent-3"), "provider": "discord", "message_id": "absent-3"}, None, ReceiptIntegrityError, "existing committed nutrition identity"),
    ],
)
def test_correction_requires_exact_identity(tmp_path, payload, identity, expected_exc, match):
    db = tmp_path / "identity.duckdb"
    migrate_database(db)
    committed_identity = ("discord", "corr-2")
    ingest_nutrition(db, _meal("baseline", message_id=committed_identity[1]), identity=committed_identity)

    with pytest.raises(expected_exc, match=match):
        correct_nutrition(db, payload, identity=identity)


def test_correction_rolls_back_when_digest_update_fails(monkeypatch, tmp_path):
    db = tmp_path / "rollback.duckdb"
    migrate_database(db)
    identity = ("discord", "corr-3")
    ingest_nutrition(db, _meal("baseline", message_id=identity[1]), identity=identity)
    before = _receipt_state(db, identity)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("nutrition_ingest._digest", boom)
    with pytest.raises(RuntimeError, match="boom"):
        correct_nutrition(db, {**_meal("changed", message_id=identity[1]), "calories": 90}, identity=identity)

    after = _receipt_state(db, identity)
    assert after == before
