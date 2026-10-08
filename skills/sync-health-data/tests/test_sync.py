"""Tests for sync-health-data skill: DB schema, imports, hash detection, validation, libre."""

import os
import sys
import subprocess
from datetime import datetime, timedelta
import tempfile
from pathlib import Path

import pytest
import duckdb

from bootstrap.env import db_path

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "sync-health-data" / "scripts"


def run_cmd(cmd, cwd=None):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd or str(REPO_ROOT))
    return r.returncode, r.stdout.strip(), r.stderr.strip()


@pytest.fixture
def db_conn():
    p = db_path()
    if not p.exists():
        pytest.skip(f"Database not found at {p}")
    conn = duckdb.connect(str(p), read_only=True)
    yield conn
    conn.close()


# ── DB Schema ────────────────────────────────────────────

def test_required_tables_exist(db_conn):
    tables = [r[0] for r in db_conn.execute("SHOW TABLES").fetchall()]
    for table in ["readings", "imports", "workouts", "nutrition_log", "medications"]:
        assert table in tables, f"Missing table: {table}"


def test_readings_schema(db_conn):
    cols = {r[1] for r in db_conn.execute("PRAGMA table_info(readings)").fetchall()}
    for col in ["timestamp", "metric", "value", "unit", "source"]:
        assert col in cols


def test_imports_schema(db_conn):
    cols = {r[1] for r in db_conn.execute("PRAGMA table_info(imports)").fetchall()}
    for col in ["filename", "imported_at", "rows_added", "source", "file_hash"]:
        assert col in cols


def test_workouts_schema(db_conn):
    cols = {r[1] for r in db_conn.execute("PRAGMA table_info(workouts)").fetchall()}
    for col in ["start_time", "end_time", "type", "duration_seconds"]:
        assert col in cols


def test_medications_schema(db_conn):
    cols = {r[1] for r in db_conn.execute("PRAGMA table_info(medications)").fetchall()}
    for col in ["timestamp", "medication", "dosage", "status"]:
        assert col in cols


def test_readings_has_data(db_conn):
    count = db_conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    assert count > 0, "readings table is empty"


def test_imports_has_data(db_conn):
    count = db_conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
    assert count > 0, "imports table is empty"


def test_no_future_timestamps(db_conn):
    future = db_conn.execute(
        "SELECT COUNT(*) FROM readings WHERE timestamp > CURRENT_TIMESTAMP + INTERVAL '1 hour'"
    ).fetchone()[0]
    assert future == 0, f"{future} future rows in readings"


def test_expected_sources(db_conn):
    valid_sources = {'healthkit', 'libre', 'lab', 'manual'}
    actual_sources = {r[0] for r in db_conn.execute("SELECT DISTINCT source FROM readings").fetchall()}
    unexpected = actual_sources - valid_sources
    assert len(unexpected) == 0, f"Unexpected sources: {unexpected}"


def test_nightly_signals_view(db_conn):
    try:
        count = db_conn.execute("SELECT COUNT(*) FROM v_nightly_signals").fetchone()[0]
        assert count >= 0
    except Exception as e:
        pytest.fail(f"v_nightly_signals view broken: {e}")


def test_nutrition_id_sequence(db_conn):
    seqs = db_conn.execute(
        "SELECT sequence_name FROM duckdb_sequences() WHERE sequence_name = 'seq_nutrition_id'"
    ).fetchall()
    assert len(seqs) > 0, "seq_nutrition_id not found"


# ── Script Imports ───────────────────────────────────────

SYNC_MODULES = ["validate", "daily_import", "sync_libre",
                "import_healthkit", "import_libre", "import_medications",
                "import_workouts", "import_cycletracking"]


@pytest.mark.parametrize("module_name", SYNC_MODULES)
def test_script_import(module_name):
    rc, _, stderr = run_cmd(
        f"{sys.executable} -c \"import sys; sys.path.insert(0, '{SCRIPTS_DIR}'); import {module_name}\"")
    assert rc == 0, f"Failed to import {module_name}: {stderr[:120]}"


# ── Hash Detection ───────────────────────────────────────

def test_hash_detection():
    sys.path.insert(0, str(SCRIPTS_DIR))
    from daily_import import calculate_file_hash

    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
        f.write("timestamp,metric,value\n2026-01-01,HR,72\n")
        f.flush()
        path1 = Path(f.name)

    hash1 = calculate_file_hash(path1)
    assert len(hash1) == 64 and all(c in '0123456789abcdef' for c in hash1)
    assert calculate_file_hash(path1) == hash1, "Same file should give same hash"

    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
        f.write("timestamp,metric,value\n2026-01-01,HR,73\n")
        f.flush()
        path2 = Path(f.name)

    assert calculate_file_hash(path2) != hash1, "Different content should give different hash"
    path1.unlink()
    path2.unlink()


# ── Validation ───────────────────────────────────────────

def _synthetic_readings_db(path, days):
    """Create a minimal readings DB with one healthy reading per given day offset."""
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE readings(timestamp TIMESTAMP, metric VARCHAR, value DOUBLE, source VARCHAR)")
    today = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    conn.executemany(
        "INSERT INTO readings VALUES (?, 'Resting Heart Rate', 55, 'healthkit')",
        [(today - timedelta(days=offset),) for offset in days],
    )
    conn.close()


def _run_validate(db_file, *flags):
    env = {**os.environ, "HEALTH_DB_PATH": str(db_file), "PYTHONPATH": str(REPO_ROOT)}
    r = subprocess.run([sys.executable, str(SCRIPTS_DIR / "validate.py"), *flags],
                       capture_output=True, text=True, cwd=str(REPO_ROOT), env=env)
    return r.returncode, r.stdout, r.stderr


def test_validate_clean_synthetic_db_exits_zero(tmp_path):
    db_file = tmp_path / "health.duckdb"
    _synthetic_readings_db(db_file, days=[1, 2, 3])
    rc, stdout, stderr = _run_validate(db_file)
    assert rc == 0, f"validate.py failed: {(stderr or stdout)[:200]}"
    assert "No data quality issues found" in stdout


def test_validate_reports_gap_and_exits_one(tmp_path):
    db_file = tmp_path / "health.duckdb"
    _synthetic_readings_db(db_file, days=[1, 3])
    rc, stdout, stderr = _run_validate(db_file, "--verbose")
    assert rc == 1, stdout
    assert "Missing data for 1 day(s)" in stdout
    assert "Date coverage:" in stdout  # --verbose prints info lines
    assert "Traceback" not in stderr


def test_validate_runs_on_live_db():
    # Live data legitimately has gaps (exit 1 = warnings found), so only require
    # that the validator completes and prints its report without crashing.
    if not db_path().exists():
        pytest.skip(f"Database not found at {db_path()}")
    for flags in ("", " --verbose"):
        rc, stdout, stderr = run_cmd(f"{sys.executable} {SCRIPTS_DIR}/validate.py{flags}")
        assert rc in (0, 1), f"validate.py crashed: {(stderr or stdout)[:200]}"
        assert "DATA QUALITY VALIDATION" in stdout
        assert "Traceback" not in stderr


# ── Import Dry-Run ───────────────────────────────────────

def test_import_dryrun():
    rc, stdout, stderr = run_cmd(f"{sys.executable} {SCRIPTS_DIR}/daily_import.py --dry-run")
    assert rc == 0, f"daily_import.py --dry-run failed: {(stderr or stdout)[:120]}"


# ── Libre Dry-Run ────────────────────────────────────────

def test_libre_dryrun():
    rc, stdout, stderr = run_cmd(f"{sys.executable} {SCRIPTS_DIR}/sync_libre.py --dry-run")
    if rc == 0:
        assert "patient" in stdout.lower() or "dry_run" in stdout.lower()
    else:
        # Credentials not configured is OK
        combined = (stderr + stdout).lower()
        assert any(k in combined for k in ["credential", "keychain", "password", "not configured"]), \
            f"Unexpected libre error: {(stderr or stdout)[:120]}"
