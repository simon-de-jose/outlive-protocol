"""Tests for coach-strength skill: hevy tables, schema, sequences."""

import sys
import subprocess
from pathlib import Path

import pytest
import duckdb

from bootstrap.env import db_path

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "coach-strength" / "scripts"


@pytest.fixture
def db_conn():
    p = db_path()
    if not p.exists():
        pytest.skip(f"Database not found at {p}")
    conn = duckdb.connect(str(p), read_only=True)
    yield conn
    conn.close()


HEVY_TABLES = ["hevy_exercises", "hevy_workouts", "hevy_sets",
               "coach_routines", "coach_progression", "hevy_sync_state"]


@pytest.mark.parametrize("table", HEVY_TABLES)
def test_hevy_table_exists(db_conn, table):
    tables = [r[0] for r in db_conn.execute("SHOW TABLES").fetchall()]
    assert table in tables, f"Missing table: {table}"


def test_hevy_exercises_schema(db_conn):
    cols = {r[1] for r in db_conn.execute("PRAGMA table_info(hevy_exercises)").fetchall()}
    for col in ["template_id", "title", "type", "primary_muscle_group", "is_custom"]:
        assert col in cols


def test_hevy_workouts_schema(db_conn):
    cols = {r[1] for r in db_conn.execute("PRAGMA table_info(hevy_workouts)").fetchall()}
    for col in ["id", "title", "routine_id", "start_time", "end_time", "duration_seconds"]:
        assert col in cols


def test_hevy_sets_schema(db_conn):
    cols = {r[1] for r in db_conn.execute("PRAGMA table_info(hevy_sets)").fetchall()}
    for col in ["workout_id", "exercise_template_id", "exercise_name",
                "set_index", "set_type", "weight_kg", "reps", "rpe"]:
        assert col in cols


def test_coach_routines_schema(db_conn):
    cols = {r[1] for r in db_conn.execute("PRAGMA table_info(coach_routines)").fetchall()}
    for col in ["id", "hevy_routine_id", "title", "exercises"]:
        assert col in cols


def test_coach_progression_schema(db_conn):
    cols = {r[1] for r in db_conn.execute("PRAGMA table_info(coach_progression)").fetchall()}
    for col in ["exercise_template_id", "date", "estimated_1rm_kg", "total_volume_kg", "total_sets"]:
        assert col in cols


def test_hevy_exercises_has_data(db_conn):
    count = db_conn.execute("SELECT COUNT(*) FROM hevy_exercises").fetchone()[0]
    assert count > 0, "hevy_exercises is empty (not synced?)"


def test_hevy_sequences(db_conn):
    seqs = db_conn.execute(
        "SELECT sequence_name FROM duckdb_sequences() WHERE sequence_name IN "
        "('seq_hevy_set_id', 'seq_coach_prog_id')"
    ).fetchall()
    seq_names = {s[0] for s in seqs}
    assert "seq_hevy_set_id" in seq_names
    assert "seq_coach_prog_id" in seq_names


@pytest.mark.parametrize("module", ["init_hevy", "sync_hevy"])
def test_script_import(module):
    rc, _, stderr = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, '{SCRIPTS_DIR}'); import {module}"],
        capture_output=True, text=True, cwd=str(REPO_ROOT)
    ).returncode, "", ""
    # Actually capture properly
    result = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, '{SCRIPTS_DIR}'); import {module}"],
        capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    assert result.returncode == 0, f"Failed to import {module}: {result.stderr[:120]}"


def test_skill_md():
    skill_path = REPO_ROOT / "skills" / "coach-strength" / "SKILL.md"
    assert skill_path.exists()
    content = skill_path.read_text()
    assert content.startswith("---")
    assert "Hevy" in content
    assert "progressive" in content.lower() or "overload" in content.lower()


def test_env_example_has_hevy_key():
    env_example = REPO_ROOT / ".env.example"
    assert env_example.exists()
    assert "HEVY_API_KEY" in env_example.read_text()
