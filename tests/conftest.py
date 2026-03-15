"""Shared test fixtures for outlive-protocol."""

import sys
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

PASS = "\u2705"
FAIL = "\u274c"

# Shared results list for the test() helper
results = []


def test(name, condition, detail=""):
    """Record a test result (legacy helper from monolith test_suite.py)."""
    status = PASS if condition else FAIL
    results.append((status, name, detail))
    print(f"  {status} {name}" + (f" \u2014 {detail}" if detail else ""))
    return condition


def run_cmd(cmd, cwd=None):
    """Run a command, return (returncode, stdout, stderr)."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd or str(REPO_ROOT))
    return r.returncode, r.stdout.strip(), r.stderr.strip()


@pytest.fixture
def repo_root():
    return REPO_ROOT


@pytest.fixture
def db_conn():
    """Read-only DuckDB connection to the health database."""
    import duckdb
    from bootstrap.env import db_path
    p = db_path()
    if not p.exists():
        pytest.skip(f"Database not found at {p}")
    conn = duckdb.connect(str(p), read_only=True)
    yield conn
    conn.close()
