"""Tests for the deterministic quick text nutrition logger."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "log-nutrition" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from nutrition_ingest import migrate_database  # noqa: E402
from quick_log_text import quick_log_text  # noqa: E402


def _seed_prior_items(db: Path) -> None:
    migrate_database(db)
    conn = duckdb.connect(str(db))
    food_items = [
        {"item": "baguette", "portion_g": 30, "calories": 82, "protein_g": 2.7, "carbs_g": 16.8, "fat_total_g": 0.5, "fat_saturated_g": 0.1, "fat_unsaturated_g": 0.4, "fat_trans_g": 0, "fiber_g": 0.8, "sugar_g": 0.9, "sodium_mg": 170, "cholesterol_mg": 0},
        {"item": "avocado", "portion_g": 75, "calories": 120, "protein_g": 1.5, "carbs_g": 6.4, "fat_total_g": 11.0, "fat_saturated_g": 1.6, "fat_unsaturated_g": 8.9, "fat_trans_g": 0, "fiber_g": 5.0, "sugar_g": 0.5, "sodium_mg": 5, "cholesterol_mg": 0},
        {"item": "hard-boiled egg", "portion_g": 50, "calories": 78, "protein_g": 6.3, "carbs_g": 0.6, "fat_total_g": 5.3, "fat_saturated_g": 1.6, "fat_unsaturated_g": 3.2, "fat_trans_g": 0, "fiber_g": 0, "sugar_g": 0.6, "sodium_mg": 62, "cholesterol_mg": 186},
        {"item": "black coffee", "portion_g": 240, "calories": 2, "protein_g": 0.3, "carbs_g": 0, "fat_total_g": 0, "fat_saturated_g": 0, "fat_unsaturated_g": 0, "fat_trans_g": 0, "fiber_g": 0, "sugar_g": 0, "sodium_mg": 5, "cholesterol_mg": 0},
    ]
    totals = {"calories": 282, "protein_g": 10.8, "carbs_g": 23.8, "fat_total_g": 16.8, "fat_saturated_g": 3.3, "fat_unsaturated_g": 12.5, "fat_trans_g": 0, "fiber_g": 5.8, "sugar_g": 2.0, "sodium_mg": 242, "cholesterol_mg": 186}
    conn.execute(
        """
        INSERT INTO nutrition_log (
            entry_id, meal_time, meal_type, meal_name, meal_description, food_items,
            calories, protein_g, carbs_g, fat_total_g, fat_saturated_g, fat_unsaturated_g,
            fat_trans_g, fiber_g, sugar_g, sodium_mg, cholesterol_mg, source, notes
        ) VALUES (nextval('seq_nutrition_entry'), '2026-06-24 08:00', 'breakfast',
            'baguette avocado egg coffee', 'seed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'test-seed', NULL)
        """,
        [json.dumps(food_items), *[totals[k] for k in ("calories", "protein_g", "carbs_g", "fat_total_g", "fat_saturated_g", "fat_unsaturated_g", "fat_trans_g", "fiber_g", "sugar_g", "sodium_mg", "cholesterol_mg")]],
    )
    conn.close()


def _payload(message_id: str = "m1") -> dict:
    return {
        "meal_time": "2026-06-25T08:10:00",
        "meal_type": "breakfast",
        "raw_text": "两片 baguette，1/3 牛油果，白煮蛋，黑咖啡",
        "discord_message_id": message_id,
        "items": [
            {"name": "baguette", "normalized_name": "baguette", "quantity": 2, "unit": "slices"},
            {"name": "牛油果", "normalized_name": "avocado", "quantity": "1/3", "unit": ""},
            {"name": "白煮蛋", "normalized_name": "hard-boiled egg", "quantity": 1, "unit": "egg"},
            {"name": "黑咖啡", "normalized_name": "black coffee", "quantity": 1, "unit": "cup"},
        ],
    }


def test_mixed_chinese_english_ingredient_reuse(monkeypatch, tmp_path):
    db = tmp_path / "health.duckdb"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "user-profile.yaml").write_text("nutrition_defaults:\n  egg: hard-boiled\n  coffee: black\n")
    monkeypatch.setenv("HEALTH_DB_PATH", str(db))
    monkeypatch.setenv("HEALTH_DATA_DIR", str(data_dir))
    _seed_prior_items(db)

    result = quick_log_text(_payload())

    assert result["status"] == "logged"
    assert result["mode"] == "ingredient_reuse"
    assert result["defaults_loaded"] is True
    assert [item["item"] for item in result["items"]] == ["baguette", "avocado", "hard-boiled egg", "black coffee"]
    assert result["totals"]["calories"] == 324
    assert result["entry"]["entry_id"] > 1


def test_idempotency_returns_existing_entry(monkeypatch, tmp_path):
    db = tmp_path / "health.duckdb"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("HEALTH_DB_PATH", str(db))
    monkeypatch.setenv("HEALTH_DATA_DIR", str(data_dir))
    _seed_prior_items(db)

    first = quick_log_text(_payload("same-message"))
    second = quick_log_text(_payload("same-message"))

    assert first["status"] == "logged"
    assert second == first


def test_exact_reuse(monkeypatch, tmp_path):
    db = tmp_path / "health.duckdb"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("HEALTH_DB_PATH", str(db))
    monkeypatch.setenv("HEALTH_DATA_DIR", str(data_dir))
    _seed_prior_items(db)

    result = quick_log_text({
        "meal_time": "2026-06-25T08:30:00",
        "meal_type": "breakfast",
        "meal_name": "baguette avocado egg coffee",
        "raw_text": "same breakfast again",
        "discord_message_id": "exact-1",
        "reuse_mode": "exact",
        "reuse": {"mode": "exact", "entry_id": 1},
    })

    assert result["status"] == "logged"
    assert result["mode"] == "exact_reuse"
    assert result["reused_entry_id"] == 1
    assert result["entry"]["calories"] == 282


def test_unknown_item_needs_clarification(monkeypatch, tmp_path):
    db = tmp_path / "health.duckdb"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("HEALTH_DB_PATH", str(db))
    monkeypatch.setenv("HEALTH_DATA_DIR", str(data_dir))
    _seed_prior_items(db)

    result = quick_log_text({
        "meal_time": "2026-06-25T12:00:00",
        "meal_type": "lunch",
        "raw_text": "mystery stew",
        "discord_message_id": "unknown-1",
        "items": [{"name": "mystery stew", "normalized_name": "mystery stew", "quantity": 1, "unit": "bowl"}],
    })

    assert result["status"] == "needs_clarification"
    assert result["reasons"][0]["reason"] in {"missing_or_unknown_quantity", "no_prior_nutrition"}


@pytest.mark.parametrize("field", ["calories", "protein_g", "carbs_g", "fat_total_g"])
def test_explicit_macro_booleans_need_clarification_without_writing(monkeypatch, tmp_path, field):
    db = tmp_path / f"bool-{field}.duckdb"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("HEALTH_DB_PATH", str(db))
    monkeypatch.setenv("HEALTH_DATA_DIR", str(data_dir))
    migrate_database(db)
    item = {
        "name": "egg", "portion_g": 50, "calories": 78,
        "protein_g": 6.3, "carbs_g": 0.6, "fat_total_g": 5.3,
    }
    item[field] = True

    result = quick_log_text({
        "meal_time": "2026-06-25T08:10:00",
        "discord_message_id": f"bool-{field}",
        "items": [item],
    })

    assert result["status"] == "needs_clarification"
    assert result["reasons"] == [{"item": "egg", "reason": "invalid_numeric_value", "fields": [field]}]
    conn = duckdb.connect(str(db), read_only=True)
    assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM nutrition_ingest_receipts").fetchone() == (0,)
    conn.close()


@pytest.mark.parametrize("field", ["portion_g", "portion", "quantity"])
def test_portion_and_quantity_booleans_need_clarification_without_writing(monkeypatch, tmp_path, field):
    db = tmp_path / f"bool-{field}.duckdb"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("HEALTH_DB_PATH", str(db))
    monkeypatch.setenv("HEALTH_DATA_DIR", str(data_dir))
    migrate_database(db)
    item = {"name": "egg", field: True}

    result = quick_log_text({
        "meal_time": "2026-06-25T08:10:00",
        "discord_message_id": f"bool-{field}",
        "items": [item],
    })

    assert result["status"] == "needs_clarification"
    assert result["reasons"] == [{"item": "egg", "reason": "invalid_numeric_value", "fields": [field]}]
    conn = duckdb.connect(str(db), read_only=True)
    assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM nutrition_ingest_receipts").fetchone() == (0,)
    conn.close()


@pytest.mark.parametrize("field,value", [
    ("calories", float("nan")),
    ("protein_g", "Infinity"),
    ("portion_g", float("-inf")),
    ("quantity", "1e309"),
    ("quantity", "1/0"),
    ("quantity", {"numerator": True, "denominator": 2}),
])
def test_invalid_or_nonfinite_quick_numbers_need_clarification_without_writing(monkeypatch, tmp_path, field, value):
    db = tmp_path / f"invalid-{field}.duckdb"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("HEALTH_DB_PATH", str(db))
    monkeypatch.setenv("HEALTH_DATA_DIR", str(data_dir))
    migrate_database(db)
    item = {"name": "egg", field: value}

    result = quick_log_text({
        "meal_time": "2026-06-25T08:10:00",
        "discord_message_id": f"invalid-{field}-{repr(value)}",
        "items": [item],
    })

    assert result["status"] == "needs_clarification"
    assert result["reasons"][0]["reason"] == "invalid_numeric_value"
    assert field in result["reasons"][0]["fields"]
    conn = duckdb.connect(str(db), read_only=True)
    assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM nutrition_ingest_receipts").fetchone() == (0,)
    conn.close()


@pytest.mark.parametrize("quantity", [2, "2", "1/2"])
def test_normal_numeric_and_string_quantities_keep_prior_scaling(monkeypatch, tmp_path, quantity):
    db = tmp_path / "health.duckdb"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("HEALTH_DB_PATH", str(db))
    monkeypatch.setenv("HEALTH_DATA_DIR", str(data_dir))
    _seed_prior_items(db)

    result = quick_log_text({
        "meal_time": "2026-06-25T08:10:00",
        "discord_message_id": f"normal-{quantity}",
        "items": [{"name": "egg", "quantity": quantity, "unit": "egg"}],
    })

    expected_multiplier = float(quantity) if quantity != "1/2" else 0.5
    assert result["status"] == "logged"
    assert result["items"][0]["portion_g"] == 50 * expected_multiplier
    assert result["totals"]["calories"] == round(78 * expected_multiplier)


def test_no_kb_or_journal_files_touched(monkeypatch, tmp_path):
    db = tmp_path / "health.duckdb"
    data_dir = tmp_path / "data"
    kb_dir = data_dir / "knowledge-base"
    journal_dir = data_dir / "journal"
    kb_dir.mkdir(parents=True)
    journal_dir.mkdir()
    sentinel = kb_dir / "sentinel.md"
    sentinel.write_text("do not touch")
    old_mtime = sentinel.stat().st_mtime_ns
    monkeypatch.setenv("HEALTH_DB_PATH", str(db))
    monkeypatch.setenv("HEALTH_DATA_DIR", str(data_dir))
    _seed_prior_items(db)

    result = quick_log_text(_payload("no-kb"))

    assert result["status"] == "logged"
    assert sentinel.read_text() == "do not touch"
    assert sentinel.stat().st_mtime_ns == old_mtime
    assert list(journal_dir.iterdir()) == []


def test_fast_benchmark_smoke(monkeypatch, tmp_path):
    db = tmp_path / "health.duckdb"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("HEALTH_DB_PATH", str(db))
    monkeypatch.setenv("HEALTH_DATA_DIR", str(data_dir))
    _seed_prior_items(db)

    start = time.perf_counter()
    result = quick_log_text(_payload("bench-1"))
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert result["status"] == "logged"
    # Keep the test generous to avoid CI flake; exact local timing is reported separately.
    assert elapsed_ms < 500
