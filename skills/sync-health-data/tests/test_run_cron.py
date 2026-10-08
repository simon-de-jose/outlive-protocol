"""Focused truthfulness tests for the stable health-sync cron entrypoint."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "skills" / "sync-health-data" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import run_cron  # noqa: E402


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    db_file = tmp_path / "health.duckdb"
    now = datetime.now()
    db = duckdb.connect(str(db_file))
    db.execute("CREATE TABLE readings(timestamp TIMESTAMP, source VARCHAR)")
    db.executemany(
        "INSERT INTO readings VALUES (?, ?)",
        [
            (now - timedelta(hours=2), "healthkit"),
            (now - timedelta(hours=1), "libre"),
        ],
    )
    db.close()

    stats = {
        "total": 0,
        "new": 0,
        "changed": 0,
        "skipped": 0,
        "imported": 0,
        "errors": 0,
        "rows_added": 0,
    }

    monkeypatch.setattr(run_cron, "db_path", lambda: db_file)
    monkeypatch.setattr(run_cron, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(run_cron, "icloud_folder", lambda: tmp_path / "icloud")
    monkeypatch.setattr(run_cron, "run_brctl", lambda path: (0, ""))
    monkeypatch.setattr(run_cron, "run_daily_import", lambda dry_run=False: stats.copy())
    monkeypatch.setattr(run_cron, "move_imported_files", lambda dry_run=False: None)
    monkeypatch.setattr(run_cron, "print_summary", lambda result: None)
    monkeypatch.setattr(
        run_cron,
        "sync_libre",
        lambda use_graph=True, dry_run=False: {"status": "success", "inserted": 0},
    )
    monkeypatch.setattr(run_cron, "run_validation", lambda verbose=False: None)
    monkeypatch.setattr(run_cron, "validate_counts", lambda: ([], "[]"))
    monkeypatch.setattr(
        run_cron,
        "capture_with_timeout",
        lambda func, timeout, *args, **kwargs: run_cron.capture(func, *args, **kwargs),
    )
    return db_file


def test_no_new_files_succeeds_only_when_healthkit_db_is_fresh(cron_env, capsys):
    rc = run_cron.main(["--skip-icloud", "--healthkit-max-age-hours", "4"])
    summary = capsys.readouterr().out

    assert rc == 0
    assert summary.startswith("🟢")
    assert "HealthKit OK: 0 files, 0 rows" in summary
    assert "Libre OK: 0 new readings" in summary

    db = duckdb.connect(str(cron_env))
    db.execute(
        "UPDATE readings SET timestamp = ? WHERE source = 'healthkit'",
        [datetime.now() - timedelta(hours=5)],
    )
    db.close()

    rc = run_cron.main(["--skip-icloud", "--healthkit-max-age-hours", "4"])
    summary = capsys.readouterr().out

    assert rc == 1
    assert summary.startswith("🔴")
    assert "HealthKit STALE" in summary
    assert "DB stale" in summary
    assert "Libre OK" in summary
    assert "(issues: HealthKit:" in summary


def test_brctl_failure_is_red_and_nonzero(cron_env, monkeypatch, capsys):
    monkeypatch.setattr(run_cron, "run_brctl", lambda path: (1, "iCloud error"))

    rc = run_cron.main([])
    summary = capsys.readouterr().out

    assert rc == 1
    assert summary.startswith("🔴")
    assert "HealthKit FAILED" in summary
    assert "brctl failed (1)" in summary
    assert "🟡" not in summary


@pytest.mark.parametrize(
    ("timed_out_function", "expected_error"),
    [
        ("run_daily_import", "file scan timed out"),
        ("move_imported_files", "file move timed out"),
    ],
)
def test_healthkit_timeouts_are_red_and_nonzero(
    cron_env, monkeypatch, capsys, timed_out_function, expected_error
):
    def capture_or_timeout(func, timeout, *args, **kwargs):
        if func is getattr(run_cron, timed_out_function):
            raise TimeoutError("simulated timeout")
        return run_cron.capture(func, *args, **kwargs)

    monkeypatch.setattr(run_cron, "capture_with_timeout", capture_or_timeout)

    rc = run_cron.main(["--skip-icloud"])
    summary = capsys.readouterr().out

    assert rc == 1
    assert summary.startswith("🔴")
    assert "HealthKit FAILED" in summary
    assert expected_error in summary
    assert "🟡" not in summary


def test_freshness_uses_each_sources_max_timestamp_and_threshold(cron_env):
    db = duckdb.connect(str(cron_env))
    newest_healthkit = datetime.now() - timedelta(minutes=30)
    db.executemany(
        "INSERT INTO readings VALUES (?, ?)",
        [
            (datetime.now() - timedelta(days=30), "healthkit"),
            (newest_healthkit, "healthkit"),
            (datetime.now() - timedelta(hours=2), "libre"),
        ],
    )
    db.close()

    result = run_cron.check_db_freshness(
        healthkit_max_age_hours=0.25,
        libre_max_age_hours=4,
        now=datetime.now(),
    )

    assert result["healthkit"].latest == newest_healthkit
    assert result["healthkit"].is_fresh is False
    assert result["libre"].is_fresh is True


def test_missing_libre_source_is_reported_separately(cron_env, capsys):
    db = duckdb.connect(str(cron_env))
    db.execute("DELETE FROM readings WHERE source = 'libre'")
    db.close()

    rc = run_cron.main(["--skip-icloud"])
    summary = capsys.readouterr().out

    assert rc == 1
    assert "HealthKit OK" in summary
    assert "Libre FAILED" in summary
    assert "DB has no libre readings" in summary
