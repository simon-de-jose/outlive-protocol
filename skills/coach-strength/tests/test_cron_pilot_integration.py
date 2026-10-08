"""Focused integration tests for the env-gated pilot in the reliable Hevy cron."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

SCRIPT_DIR = Path(__file__).parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def integrated_gate(tmp_path, monkeypatch):
    sync = load("sync_hevy")
    gate = load("hevy_sync_cron")
    db_path = tmp_path / "health.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SEQUENCE seq_hevy_set_id START 1")
    conn.execute("CREATE SEQUENCE seq_coach_prog_id START 1")
    conn.execute(
        "CREATE TABLE hevy_sync_state(key VARCHAR PRIMARY KEY,value VARCHAR,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute("CREATE TABLE hevy_exercises(template_id VARCHAR PRIMARY KEY,title VARCHAR)")
    conn.execute(
        """CREATE TABLE hevy_workouts(
               id VARCHAR PRIMARY KEY,title VARCHAR,routine_id VARCHAR,description VARCHAR,
               start_time TIMESTAMP,end_time TIMESTAMP,duration_seconds INTEGER,
               created_at TIMESTAMP,updated_at TIMESTAMP,synced_at TIMESTAMP)"""
    )
    conn.execute(
        """CREATE TABLE hevy_sets(
               id INTEGER PRIMARY KEY DEFAULT nextval('seq_hevy_set_id'),
               workout_id VARCHAR,exercise_template_id VARCHAR,exercise_name VARCHAR,
               set_index INTEGER,set_type VARCHAR,weight_kg DOUBLE,reps INTEGER,
               distance_meters DOUBLE,duration_seconds DOUBLE,rpe DOUBLE,custom_metric DOUBLE)"""
    )
    conn.execute(
        """CREATE TABLE coach_routines(
               id VARCHAR PRIMARY KEY,hevy_routine_id VARCHAR,title VARCHAR,
               split_type VARCHAR,day_label VARCHAR,exercises VARCHAR,
               created_at TIMESTAMP,updated_at TIMESTAMP)"""
    )
    conn.execute(
        """CREATE TABLE coach_progression(
               id INTEGER PRIMARY KEY DEFAULT nextval('seq_coach_prog_id'),
               exercise_template_id VARCHAR,date DATE,estimated_1rm_kg DOUBLE,
               best_set_weight_kg DOUBLE,best_set_reps INTEGER,total_volume_kg DOUBLE,
               total_sets INTEGER)"""
    )

    load_config = {}
    for key, approved in gate.strength_pilot.PILOT_ROUTINES.items():
        exercises = []
        for index, template_id in enumerate(approved["exercise_template_ids"]):
            exercises.append(
                {
                    "index": index,
                    "title": f"Exercise {template_id}",
                    "exercise_template_id": template_id,
                    "sets": [
                        {
                            "index": 0,
                            "type": "normal",
                            "weight_kg": 10.0,
                            "reps": 10,
                        }
                    ],
                }
            )
            load_config[template_id] = {"increment": 2.0, "semantics": "per_hand"}
        conn.execute(
            "INSERT INTO coach_routines(id,hevy_routine_id,title,exercises) VALUES (?,?,?,?)",
            [f"local-{key}", approved["hevy_routine_id"], approved["title"], json.dumps(exercises)],
        )
    conn.close()

    profile = {
        "goals": ["longevity"],
        "days_per_week": 2,
        "equipment_constraints": ["private-profile-marker"],
        "injuries": [],
        "pain_triggers": [],
        "avoid_list": [],
        "units": "kg",
        "data_freshness_hours": 48,
        "exercise_load_config": load_config,
    }
    profile_path = tmp_path / "pilot-profile.json"
    profile_path.write_text(json.dumps(profile))

    sync.DB_PATH = db_path
    gate.sync_hevy = sync
    gate.LOCK_PATH = tmp_path / "sync.lock"
    monkeypatch.delenv("STRENGTH_PILOT_ENABLED", raising=False)
    monkeypatch.delenv("STRENGTH_PILOT_PROFILE", raising=False)
    return gate, db_path, sync, profile_path


def add_workout(conn, pilot, workout_id, key, start, end):
    approved = pilot.PILOT_ROUTINES[key]
    conn.execute(
        """INSERT INTO hevy_workouts(
               id,title,routine_id,start_time,end_time,duration_seconds,updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        [
            workout_id,
            approved["title"],
            approved["hevy_routine_id"],
            pilot.utc_naive(start),
            pilot.utc_naive(end),
            int((end - start).total_seconds()),
            pilot.utc_naive(end),
        ],
    )
    for template_id in approved["exercise_template_ids"]:
        conn.execute(
            """INSERT INTO hevy_sets(
                   workout_id,exercise_template_id,exercise_name,set_index,set_type,
                   weight_kg,reps)
               VALUES (?,?,?,?,?,?,?)""",
            [workout_id, template_id, f"Exercise {template_id}", 0, "normal", 10.0, 10],
        )


def advance(sync, conn, run_start):
    sync._advance_sync_state(conn, run_start)


def enable(monkeypatch, profile_path):
    monkeypatch.setenv("STRENGTH_PILOT_ENABLED", "true")
    monkeypatch.setenv("STRENGTH_PILOT_PROFILE", str(profile_path))


def test_disabled_behavior_ignores_pilot_profile(integrated_gate, monkeypatch, capsys):
    gate, _, sync, _ = integrated_gate
    monkeypatch.setenv("STRENGTH_PILOT_PROFILE", "/definitely/not/a/profile.json")
    sync.sync_hevy = lambda **kwargs: advance(sync, kwargs["conn"], kwargs["run_start"])

    assert gate.run() == 0
    assert capsys.readouterr().out == "NO_REPLY\n"


def test_enabled_baseline_new_card_next_daily_checkin_and_duplicate_quiet(
    integrated_gate, monkeypatch, capsys
):
    gate, db_path, sync, profile_path = integrated_gate
    enable(monkeypatch, profile_path)
    clock = [datetime(2026, 8, 26, 21, 0, tzinfo=timezone.utc)]
    gate._now = lambda: clock[0]

    conn = duckdb.connect(str(db_path))
    add_workout(
        conn,
        gate.strength_pilot,
        "historical-a",
        "A",
        clock[0] - timedelta(days=1, hours=1),
        clock[0] - timedelta(days=1),
    )
    conn.close()

    calls = 0

    def fake_sync(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            add_workout(
                kwargs["conn"],
                gate.strength_pilot,
                "new-b",
                "B",
                kwargs["run_start"] - timedelta(hours=24, minutes=55),
                kwargs["run_start"] - timedelta(hours=23, minutes=55),
            )
        advance(sync, kwargs["conn"], kwargs["run_start"])

    sync.sync_hevy = fake_sync

    # Existing history is reconciled and the alternating B card is cached, but silent.
    assert gate.run() == 0
    assert capsys.readouterr().out == "NO_REPLY\n"
    conn = duckdb.connect(str(db_path))
    assert conn.execute(
        "SELECT routine_key FROM coach_strength_pilot_prescriptions"
    ).fetchall() == [("B",)]
    assert conn.execute(
        "SELECT state FROM coach_strength_pilot_checkins WHERE workout_id='historical-a'"
    ).fetchone()[0] == "suppressed_baseline"
    conn.close()

    # A workout completed just after the prior daily poll is still eligible on the next one.
    clock[0] += timedelta(days=1)
    assert gate.run() == 0
    output = capsys.readouterr().out
    assert "🏋️ Outlive B — Pull Emphasis" in output
    assert "Outlive A — Push Emphasis — next session" in output
    assert "Quick check:" in output
    assert "private-profile-marker" not in output
    assert "new-b" not in output

    # The one-shot prompt is still quiet on the very next run once sent.
    clock[0] += timedelta(minutes=5)
    assert gate.run() == 0
    assert capsys.readouterr().out.strip() == "NO_REPLY"

    # It is never retried, and an unchanged subsequent run is exactly quiet.
    clock[0] += timedelta(minutes=1)
    assert gate.run() == 0
    assert capsys.readouterr().out == "NO_REPLY\n"


def test_missing_or_invalid_profile_fails_closed(
    integrated_gate, monkeypatch, capsys, tmp_path
):
    gate, db_path, sync, _ = integrated_gate
    monkeypatch.setenv("STRENGTH_PILOT_ENABLED", "true")
    called = []
    sync.sync_hevy = lambda **kwargs: called.append(True)

    for value in (None, tmp_path / "missing.json", tmp_path / "invalid.json"):
        if value is None:
            monkeypatch.delenv("STRENGTH_PILOT_PROFILE", raising=False)
        else:
            if value.name == "invalid.json":
                value.write_text("{private-invalid-json")
            monkeypatch.setenv("STRENGTH_PILOT_PROFILE", str(value))
        assert gate.run() == 1
        output = capsys.readouterr().out
        assert "HEVY_SYNC_FAILED" in output
        assert "error_class=configuration" in output
        assert "private-invalid-json" not in output

    assert called == []

    # Syntactically valid JSON that is not a valid pilot profile also fails,
    # after which both sync and pilot writes are rolled back.
    semantic_invalid = tmp_path / "semantic-invalid.json"
    semantic_invalid.write_text("{}")
    monkeypatch.setenv("STRENGTH_PILOT_PROFILE", str(semantic_invalid))

    def advance_then_track(**kwargs):
        called.append(True)
        advance(sync, kwargs["conn"], kwargs["run_start"])

    sync.sync_hevy = advance_then_track
    assert gate.run() == 1
    output = capsys.readouterr().out
    assert "error_class=configuration" in output
    assert called == [True]

    conn = duckdb.connect(str(db_path))
    assert "coach_strength_pilot_results" not in {
        row[0] for row in conn.execute("SHOW TABLES").fetchall()
    }
    assert conn.execute(
        "SELECT value FROM hevy_sync_state WHERE key='last_sync'"
    ).fetchone() is None
    conn.close()


def test_pilot_failure_atomically_rolls_back_sync_and_pilot_state(
    integrated_gate, monkeypatch, capsys
):
    gate, db_path, sync, profile_path = integrated_gate
    enable(monkeypatch, profile_path)
    clock = [datetime(2026, 8, 26, 18, 0, tzinfo=timezone.utc)]
    gate._now = lambda: clock[0]
    sync.sync_hevy = lambda **kwargs: advance(sync, kwargs["conn"], kwargs["run_start"])
    assert gate.run() == 0
    capsys.readouterr()

    clock[0] += timedelta(hours=1)

    def sync_new(**kwargs):
        add_workout(
            kwargs["conn"],
            gate.strength_pilot,
            "rolled-back-b",
            "B",
            clock[0] - timedelta(hours=1),
            clock[0],
        )
        advance(sync, kwargs["conn"], kwargs["run_start"])

    sync.sync_hevy = sync_new
    original = gate.strength_pilot.generate_card

    def fail_card(*args, **kwargs):
        raise gate.strength_pilot.PilotDataError("pilot integration failed safely")

    monkeypatch.setattr(gate.strength_pilot, "generate_card", fail_card)
    assert gate.run() == 1
    assert "error_class=pilot_data" in capsys.readouterr().out
    monkeypatch.setattr(gate.strength_pilot, "generate_card", original)

    conn = duckdb.connect(str(db_path))
    assert conn.execute(
        "SELECT COUNT(*) FROM hevy_workouts WHERE id='rolled-back-b'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM coach_strength_pilot_results WHERE workout_id='rolled-back-b'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM coach_strength_pilot_checkins WHERE workout_id='rolled-back-b'"
    ).fetchone()[0] == 0
    state = json.loads(
        conn.execute(
            "SELECT value FROM hevy_sync_state WHERE key=?", [gate.STATE_KEY]
        ).fetchone()[0]
    )
    assert state["freshness"] is False
    assert state["ever_observed_completed_workout_ids"] == []
    conn.close()


def test_output_never_echoes_profile_or_stored_sensitive_feedback(
    integrated_gate, monkeypatch, capsys
):
    gate, db_path, sync, profile_path = integrated_gate
    enable(monkeypatch, profile_path)
    clock = [datetime(2026, 8, 26, 18, 0, tzinfo=timezone.utc)]
    gate._now = lambda: clock[0]

    conn = duckdb.connect(str(db_path))
    add_workout(
        conn,
        gate.strength_pilot,
        "private-a",
        "A",
        clock[0] - timedelta(days=1, hours=1),
        clock[0] - timedelta(days=1),
    )
    conn.close()
    calls = 0

    def fake_sync(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            add_workout(
                kwargs["conn"],
                gate.strength_pilot,
                "public-b",
                "B",
                clock[0] - timedelta(hours=1),
                clock[0],
            )
        advance(sync, kwargs["conn"], kwargs["run_start"])

    sync.sync_hevy = fake_sync
    assert gate.run() == 0
    capsys.readouterr()

    conn = duckdb.connect(str(db_path))
    gate.strength_pilot.record_feedback(
        conn,
        "private-a",
        "yes",
        change_requested=True,
        change_note="stored-sensitive-feedback-marker",
        now=clock[0],
    )
    conn.commit()
    conn.close()

    clock[0] += timedelta(hours=1)
    assert gate.run() == 0
    output = capsys.readouterr().out
    assert "private-profile-marker" not in output
    assert "stored-sensitive-feedback-marker" not in output
