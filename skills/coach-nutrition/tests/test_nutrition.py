"""Tests for coach-nutrition skill: nutrition schema, data sanity."""

import pytest
import duckdb

from bootstrap.env import db_path


@pytest.fixture
def db_conn():
    p = db_path()
    if not p.exists():
        pytest.skip(f"Database not found at {p}")
    conn = duckdb.connect(str(p), read_only=True)
    yield conn
    conn.close()


def test_nutrition_log_schema(db_conn):
    cols = {r[1] for r in db_conn.execute("PRAGMA table_info(nutrition_log)").fetchall()}
    expected = {"entry_id", "meal_time", "meal_type", "meal_name", "calories",
                "protein_g", "carbs_g", "fat_total_g", "food_items", "source"}
    missing = expected - cols
    assert len(missing) == 0, f"Missing columns: {missing}"


def test_nutrition_values_numeric(db_conn):
    tables = [r[0] for r in db_conn.execute("SHOW TABLES").fetchall()]
    if "nutrition_log" not in tables:
        pytest.skip("nutrition_log table not found")
    sample = db_conn.execute(
        "SELECT calories, protein_g, carbs_g, fat_total_g FROM nutrition_log LIMIT 1"
    ).fetchone()
    if sample:
        assert all(isinstance(v, (int, float)) or v is None for v in sample)
        if sample[0] is not None:
            assert 0 < sample[0] < 5000, f"Calories out of range: {sample[0]}"


def test_nutrition_summary_imports():
    """nutrition_summary.py can be imported."""
    import sys
    from pathlib import Path
    scripts_dir = str(Path(__file__).parent.parent / "scripts")
    sys.path.insert(0, scripts_dir)
    import nutrition_summary  # noqa: F401
