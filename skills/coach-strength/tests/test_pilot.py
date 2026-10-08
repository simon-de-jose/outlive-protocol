"""Focused end-to-end tests for the deterministic two-week strength pilot."""
from __future__ import annotations

import ast
import importlib.util
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "strength_pilot.py"
SPEC = importlib.util.spec_from_file_location("strength_pilot", SCRIPT)
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


def utc(year=2026, month=8, day=20, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    db = duckdb.connect(str(tmp_path / "pilot.duckdb"))
    db.execute("CREATE SEQUENCE seq_hevy_set_id START 1")
    db.execute("""
        CREATE TABLE hevy_sync_state(
            key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMP)
    """)
    db.execute("""
        CREATE TABLE coach_routines(
            id VARCHAR PRIMARY KEY, hevy_routine_id VARCHAR, title VARCHAR,
            split_type VARCHAR, day_label VARCHAR, exercises VARCHAR,
            created_at TIMESTAMP, updated_at TIMESTAMP)
    """)
    db.execute("""
        CREATE TABLE hevy_workouts(
            id VARCHAR PRIMARY KEY, title VARCHAR, routine_id VARCHAR,
            description VARCHAR, start_time TIMESTAMP, end_time TIMESTAMP,
            duration_seconds INTEGER, created_at TIMESTAMP, updated_at TIMESTAMP,
            synced_at TIMESTAMP)
    """)
    db.execute("""
        CREATE TABLE hevy_sets(
            id INTEGER PRIMARY KEY DEFAULT nextval('seq_hevy_set_id'),
            workout_id VARCHAR, exercise_template_id VARCHAR, exercise_name VARCHAR,
            set_index INTEGER, set_type VARCHAR, weight_kg DOUBLE, reps INTEGER,
            distance_meters DOUBLE, duration_seconds DOUBLE, rpe DOUBLE,
            custom_metric DOUBLE)
    """)
    for key, approved in pilot.PILOT_ROUTINES.items():
        exercises = [
            {
                "index": index, "title": f"Exercise {template_id}",
                "exercise_template_id": template_id,
                "sets": [{"index": 0, "type": "normal", "weight_kg": 10.0, "reps": 10}],
            }
            for index, template_id in enumerate(approved["exercise_template_ids"])
        ]
        db.execute(
            """INSERT INTO coach_routines(id, hevy_routine_id, title, exercises)
               VALUES (?, ?, ?, ?)""",
            [f"local-{key}", approved["hevy_routine_id"], approved["title"], json.dumps(exercises)],
        )
    yield db
    db.close()


def profile(key="A", *, units="kg", increment=2.0, semantics="per_hand"):
    ids = pilot.PILOT_ROUTINES[key]["exercise_template_ids"]
    return {
        "goals": ["longevity"],
        "days_per_week": 2,
        "equipment_constraints": ["dumbbells", "bench"],
        "injuries": [],
        "pain_triggers": [],
        "avoid_list": [],
        "units": units,
        "data_freshness_hours": 48,
        "exercise_load_config": {
            template_id: {"increment": increment, "semantics": semantics}
            for template_id in ids
        },
    }


def set_fresh(conn, at):
    conn.execute(
        """INSERT INTO hevy_sync_state VALUES ('last_sync', ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        [at.isoformat(), pilot.utc_naive(at)],
    )


def add_workout(
    conn, workout_id, start, *, key="A", load=10.0, reps=10,
    missed_template=None, omit_template=None, duplicate_template=None,
    extra_template=None, routine_id=True,
):
    approved = pilot.PILOT_ROUTINES[key]
    end = start + timedelta(hours=1)
    conn.execute(
        """INSERT INTO hevy_workouts(
               id,title,routine_id,start_time,end_time,duration_seconds,updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        [
            workout_id, approved["title"], approved["hevy_routine_id"] if routine_id else None,
            pilot.utc_naive(start), pilot.utc_naive(end), 3600, pilot.utc_naive(end),
        ],
    )
    for template_id in approved["exercise_template_ids"]:
        if template_id == omit_template:
            continue
        conn.execute(
            """INSERT INTO hevy_sets(
                   workout_id,exercise_template_id,exercise_name,set_index,set_type,weight_kg,reps)
               VALUES (?,?,?,?,?,?,?)""",
            [workout_id, template_id, template_id, 0, "normal", load,
             reps - 1 if template_id == missed_template else reps],
        )
    if duplicate_template:
        conn.execute(
            """INSERT INTO hevy_sets(
                   workout_id,exercise_template_id,exercise_name,set_index,set_type,weight_kg,reps)
               VALUES (?,?,?,?,?,?,?)""",
            [workout_id, duplicate_template, duplicate_template, 0, "normal", load, reps],
        )
    if extra_template:
        conn.execute(
            """INSERT INTO hevy_sets(
                   workout_id,exercise_template_id,exercise_name,set_index,set_type,weight_kg,reps)
               VALUES (?,?,?,?,?,?,?)""",
            [workout_id, extra_template, extra_template, 0, "normal", load, reps],
        )
    return end


def baseline_two(conn, now, *, second_gap_days=3, latest_age_days=2, missed_template=None):
    latest = now - timedelta(days=latest_age_days)
    earlier = latest - timedelta(days=second_gap_days)
    add_workout(conn, "w1", earlier)
    add_workout(conn, "w2", latest, missed_template=missed_template)
    set_fresh(conn, now)
    pilot.refresh_evidence(conn, now)
    pilot.record_feedback(conn, "w1", "no", now=earlier + timedelta(hours=2))
    pilot.record_feedback(conn, "w2", "no", now=latest + timedelta(hours=2))


def test_schema_migration_is_idempotent(conn):
    pilot.init_schema(conn)
    pilot.init_schema(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM coach_strength_pilot_schema WHERE version=1"
    ).fetchone()[0] == 1
    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    assert {
        "coach_strength_pilot_prescriptions", "coach_strength_pilot_results",
        "coach_strength_pilot_feedback", "coach_strength_pilot_checkins",
    } <= tables
    result_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(coach_strength_pilot_results)").fetchall()
    }
    assert {"result_id", "routine_snapshot_json", "routine_hash", "rule_version"} <= result_columns


def test_sync_match_prescribe_new_workouts_and_adapt_end_to_end(conn):
    now = utc()
    baseline_two(conn, now)

    first = pilot.generate_card(conn, "A", profile(), now)
    assert first["status"] == "increase"
    assert first["decision"]["confidence_dimensions"] == {
        "identity_match": "high", "data_freshness": "high", "completion": "high",
        "feedback": "high", "comparability": "high",
    }
    assert set(first["decision"]["changed_exercise_template_ids"]) == set(
        pilot.PILOT_ROUTINES["A"]["exercise_template_ids"]
    )
    assert all(
        item["sets"][0]["load_kg"] == 12.0 for item in first["decision"]["targets"].values()
    )
    repeated = pilot.generate_card(conn, "A", profile(), now + timedelta(minutes=1))
    assert repeated["prescription_id"] == first["prescription_id"]
    assert conn.execute("SELECT COUNT(*) FROM coach_strength_pilot_prescriptions").fetchone()[0] == 1

    end3 = add_workout(conn, "w3", now + timedelta(days=1), load=12.0)
    set_fresh(conn, end3 + timedelta(minutes=5))
    refreshed = pilot.refresh_evidence(conn, end3 + timedelta(minutes=5))
    assert refreshed["inserted"] == 1
    result = json.loads(conn.execute(
        "SELECT result_json FROM coach_strength_pilot_results WHERE workout_id='w3'"
    ).fetchone()[0])
    assert result["prescription_id"] == first["prescription_id"]
    assert {item["status"] for item in result["exercises"].values()} == {"achieved"}
    pilot.record_feedback(conn, "w3", "no", now=end3 + timedelta(minutes=15))

    hold = pilot.generate_card(conn, "A", profile(), end3 + timedelta(minutes=20))
    assert hold["status"] == "hold"
    assert "NONCOMPARABLE_EXPOSURES" in hold["decision"]["reason_codes"]
    assert all(item["sets"][0]["load_kg"] == 12.0 for item in hold["decision"]["targets"].values())

    end4 = add_workout(conn, "w4", now + timedelta(days=3), load=12.0)
    set_fresh(conn, end4 + timedelta(minutes=5))
    pilot.refresh_evidence(conn, end4 + timedelta(minutes=5))
    pilot.record_feedback(conn, "w4", "no", now=end4 + timedelta(minutes=15))
    second_increase = pilot.generate_card(conn, "A", profile(), end4 + timedelta(minutes=20))
    assert second_increase["status"] == "increase"
    assert all(
        item["sets"][0]["load_kg"] == 14.0
        for item in second_increase["decision"]["targets"].values()
    )


def test_duplicate_edit_delete_and_resurrection_are_idempotent(conn):
    now = utc()
    add_workout(conn, "same-id", now - timedelta(days=1))
    set_fresh(conn, now)
    first = pilot.refresh_evidence(conn, now)
    second = pilot.refresh_evidence(conn, now + timedelta(minutes=1))
    assert first["inserted"] == 1 and second["unchanged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM coach_strength_pilot_results").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM coach_strength_pilot_checkins").fetchone()[0] == 1

    first_template = pilot.PILOT_ROUTINES["A"]["exercise_template_ids"][0]
    conn.execute(
        "UPDATE hevy_sets SET reps=9 WHERE workout_id='same-id' AND exercise_template_id=?",
        [first_template],
    )
    conn.execute("UPDATE hevy_workouts SET updated_at=? WHERE id='same-id'", [pilot.utc_naive(now)])
    edited = pilot.refresh_evidence(conn, now + timedelta(minutes=2))
    assert edited["updated"] == 1
    assert "MISSED_REPS" in json.loads(conn.execute(
        "SELECT reason_codes_json FROM coach_strength_pilot_results WHERE workout_id='same-id'"
    ).fetchone()[0])

    conn.execute("DELETE FROM hevy_sets WHERE workout_id='same-id'")
    conn.execute("DELETE FROM hevy_workouts WHERE id='same-id'")
    deleted = pilot.refresh_evidence(conn, now + timedelta(minutes=3))
    assert deleted["deleted"] == 1
    assert conn.execute(
        "SELECT source_status FROM coach_strength_pilot_results WHERE workout_id='same-id'"
    ).fetchone()[0] == "deleted"
    deleted_card = pilot.generate_card(conn, "A", profile(), now + timedelta(minutes=3))
    assert deleted_card["status"] == "review"
    assert "SOURCE_DELETED" in deleted_card["decision"]["reason_codes"]
    assert deleted_card["decision"]["changed_exercise_template_ids"] == []

    add_workout(conn, "same-id", now - timedelta(days=1))
    resurrected = pilot.refresh_evidence(conn, now + timedelta(minutes=4))
    assert resurrected["updated"] == 1
    assert conn.execute(
        "SELECT source_status FROM coach_strength_pilot_results WHERE workout_id='same-id'"
    ).fetchone()[0] == "active"
    assert conn.execute("SELECT COUNT(*) FROM coach_strength_pilot_checkins").fetchone()[0] == 1


def test_set_reinsert_and_order_changes_do_not_use_ephemeral_set_ids(conn):
    now = utc()
    add_workout(conn, "reinserted", now - timedelta(days=1))
    set_fresh(conn, now)
    pilot.refresh_evidence(conn, now)
    original_hash = conn.execute(
        "SELECT evidence_hash FROM coach_strength_pilot_results WHERE workout_id='reinserted'"
    ).fetchone()[0]
    rows = conn.execute(
        """SELECT workout_id,exercise_template_id,exercise_name,set_index,set_type,
                  weight_kg,reps,distance_meters,duration_seconds,rpe,custom_metric
           FROM hevy_sets WHERE workout_id='reinserted'"""
    ).fetchall()
    conn.execute("DELETE FROM hevy_sets WHERE workout_id='reinserted'")
    conn.executemany(
        """INSERT INTO hevy_sets(
               workout_id,exercise_template_id,exercise_name,set_index,set_type,
               weight_kg,reps,distance_meters,duration_seconds,rpe,custom_metric)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        list(reversed(rows)),
    )
    result = pilot.refresh_evidence(conn, now + timedelta(minutes=1))
    assert result["unchanged"] == 1
    assert conn.execute(
        "SELECT evidence_hash FROM coach_strength_pilot_results WHERE workout_id='reinserted'"
    ).fetchone()[0] == original_hash
    source = inspect.getsource(pilot._workout_sets)
    assert "ORDER BY id" not in source and "SELECT id" not in source


def test_pain_blocks_every_increase_and_stays_local(conn):
    now = utc()
    baseline_two(conn, now)
    feedback = pilot.record_feedback(conn, "w2", "yes", change_note="shoulder", now=now)
    card = pilot.generate_card(conn, "A", profile(), now)
    assert feedback["context_scope"] == pilot.LOCAL_CONTEXT
    assert "pain_status" not in feedback and "change_note" not in feedback
    assert card["card"]["context_scope"] == pilot.LOCAL_CONTEXT
    assert conn.execute(
        "SELECT context_scope FROM coach_strength_pilot_feedback WHERE workout_id='w2'"
    ).fetchone()[0] == pilot.LOCAL_CONTEXT
    assert card["status"] == "review"
    assert "PAIN_REPORTED" in card["decision"]["reason_codes"]
    assert card["decision"]["changed_exercise_template_ids"] == []
    assert all(item["action"] == "hold" for item in card["decision"]["exercise_decisions"].values())


def test_profile_injury_or_avoid_list_requires_review(conn):
    now = utc()
    baseline_two(conn, now)
    p = profile()
    p["injuries"] = ["unresolved shoulder issue"]
    card = pilot.generate_card(conn, "A", p, now)
    assert card["status"] == "review"
    assert card["decision"]["needs_input"] is True
    assert "PROFILE_LIMITATION_REVIEW" in card["decision"]["reason_codes"]
    assert card["decision"]["changed_exercise_template_ids"] == []


def test_missed_reps_hold_without_inferred_effort_or_form(conn):
    now = utc()
    missed = pilot.PILOT_ROUTINES["A"]["exercise_template_ids"][0]
    baseline_two(conn, now, missed_template=missed)
    card = pilot.generate_card(conn, "A", profile(), now)
    assert card["status"] == "hold"
    assert card["decision"]["exercise_decisions"][missed]["action"] == "hold"
    assert "MISSED_REPS" in card["decision"]["reason_codes"]
    serialized = json.dumps(card).lower()
    assert "form observed" not in serialized and "rir achieved" not in serialized


def test_every_planned_warmup_and_working_set_must_meet_load_and_reps(conn):
    now = utc()
    exercises = json.loads(conn.execute(
        "SELECT exercises FROM coach_routines WHERE id='local-A'"
    ).fetchone()[0])
    first = exercises[0]["exercise_template_id"]
    exercises[0]["sets"] = [
        {"index": 0, "type": "warmup", "weight_kg": 5.0, "reps": 10},
        {"index": 1, "type": "normal", "weight_kg": 10.0, "reps": 10},
    ]
    conn.execute(
        "UPDATE coach_routines SET exercises=? WHERE id='local-A'", [json.dumps(exercises)]
    )
    for workout_id, start, warmup_reps in (
        ("w1", now - timedelta(days=4), 10),
        ("w2", now - timedelta(days=1), 9),
    ):
        add_workout(conn, workout_id, start)
        conn.execute(
            """UPDATE hevy_sets SET set_index=1
               WHERE workout_id=? AND exercise_template_id=?""",
            [workout_id, first],
        )
        conn.execute(
            """INSERT INTO hevy_sets(
                   workout_id,exercise_template_id,exercise_name,set_index,set_type,weight_kg,reps)
               VALUES (?,?,?,?,?,?,?)""",
            [workout_id, first, first, 0, "warmup", 5.0, warmup_reps],
        )
    set_fresh(conn, now)
    pilot.refresh_evidence(conn, now)
    pilot.record_feedback(conn, "w1", "no", now=now)
    pilot.record_feedback(conn, "w2", "no", now=now)
    card = pilot.generate_card(conn, "A", profile(), now)
    assert card["status"] == "hold"
    assert "MISSED_REPS" in card["decision"]["reason_codes"]
    assert card["decision"]["changed_exercise_template_ids"] == []


def test_incomplete_session_holds(conn):
    now = utc()
    omitted = pilot.PILOT_ROUTINES["A"]["exercise_template_ids"][-1]
    latest = now - timedelta(days=2)
    add_workout(conn, "w1", latest - timedelta(days=3))
    add_workout(conn, "w2", latest, omit_template=omitted)
    set_fresh(conn, now)
    pilot.refresh_evidence(conn, now)
    pilot.record_feedback(conn, "w1", "no", now=now)
    pilot.record_feedback(conn, "w2", "no", now=now)
    card = pilot.generate_card(conn, "A", profile(), now)
    assert "INCOMPLETE_SESSION" in card["decision"]["reason_codes"]
    assert card["decision"]["changed_exercise_template_ids"] == []


def test_long_gap_over_ten_days_requires_review(conn):
    now = utc()
    baseline_two(conn, now, second_gap_days=11, latest_age_days=1)
    card = pilot.generate_card(conn, "A", profile(), now)
    assert card["status"] == "review"
    assert "LONG_GAP" in card["decision"]["reason_codes"]
    assert card["decision"]["changed_exercise_template_ids"] == []


def test_ambiguous_duplicate_exercise_match_requires_review(conn):
    now = utc()
    duplicate = pilot.PILOT_ROUTINES["A"]["exercise_template_ids"][0]
    add_workout(conn, "w1", now - timedelta(days=4))
    add_workout(conn, "w2", now - timedelta(days=1), duplicate_template=duplicate)
    set_fresh(conn, now)
    pilot.refresh_evidence(conn, now)
    pilot.record_feedback(conn, "w1", "no", now=now)
    pilot.record_feedback(conn, "w2", "no", now=now)
    result = conn.execute(
        "SELECT match_status FROM coach_strength_pilot_results WHERE workout_id='w2'"
    ).fetchone()[0]
    card = pilot.generate_card(conn, "A", profile(), now)
    assert result == "review"
    assert "AMBIGUOUS_MATCH" in card["decision"]["reason_codes"]
    assert card["decision"]["changed_exercise_template_ids"] == []


def test_material_exercise_change_blocks_progression(conn):
    now = utc()
    add_workout(conn, "w1", now - timedelta(days=4))
    add_workout(conn, "w2", now - timedelta(days=1), extra_template="NOT_APPROVED")
    set_fresh(conn, now)
    pilot.refresh_evidence(conn, now)
    pilot.record_feedback(conn, "w1", "no", now=now)
    pilot.record_feedback(conn, "w2", "no", now=now)
    card = pilot.generate_card(conn, "A", profile(), now)
    assert "MATERIAL_CHANGE" in card["decision"]["reason_codes"]
    assert card["decision"]["changed_exercise_template_ids"] == []


def test_confirmed_owner_profile_defaults_are_explicit(conn):
    routines = pilot.load_routines(conn)
    defaults = pilot.confirmed_profile_defaults(
        {
            "days_per_week": 2,
            "equipment_constraints": ["dumbbells", "bench"],
            "data_freshness_hours": 48,
        },
        "A",
    )
    validated = pilot.validate_profile(defaults, routines, "A")
    assert validated["goals"] == ["hypertrophy", "long-term strength"]
    assert validated["injuries"] == validated["pain_triggers"] == []
    assert validated["units"] == "lb"
    assert {
        (config["increment"], config["semantics"])
        for config in validated["exercise_load_config"].values()
    } == {(5.0, "per_hand")}


def test_missing_profile_or_unknown_load_semantics_fails_closed(conn):
    now = utc()
    set_fresh(conn, now)
    with pytest.raises(pilot.PilotConfigurationError):
        pilot.generate_card(conn, "A", None, now)
    bad = profile()
    first_id = pilot.PILOT_ROUTINES["A"]["exercise_template_ids"][0]
    bad["exercise_load_config"][first_id]["semantics"] = "unknown"
    with pytest.raises(pilot.PilotConfigurationError):
        pilot.generate_card(conn, "A", bad, now)
    assert conn.execute("SELECT COUNT(*) FROM coach_strength_pilot_prescriptions").fetchone()[0] == 0


def test_configured_units_increment_and_per_hand_total_semantics(conn):
    now = utc()
    # Use exact 20 lb source targets to make conversion assertions explicit.
    conn.execute(
        """UPDATE coach_routines SET exercises=? WHERE id='local-A'""",
        [json.dumps([
            {
                "index": index, "title": f"Exercise {template_id}",
                "exercise_template_id": template_id,
                "sets": [{"index": 0, "type": "normal", "weight_kg": 20 * pilot.KG_PER_LB, "reps": 10}],
            }
            for index, template_id in enumerate(pilot.PILOT_ROUTINES["A"]["exercise_template_ids"])
        ])],
    )
    add_workout(conn, "w1", now - timedelta(days=4), load=20 * pilot.KG_PER_LB)
    add_workout(conn, "w2", now - timedelta(days=1), load=20 * pilot.KG_PER_LB)
    set_fresh(conn, now)
    pilot.refresh_evidence(conn, now)
    pilot.record_feedback(conn, "w1", "no", now=now)
    pilot.record_feedback(conn, "w2", "no", now=now)
    p = profile(units="lb", increment=5, semantics="per_hand")
    total_id = pilot.PILOT_ROUTINES["A"]["exercise_template_ids"][-1]
    p["exercise_load_config"][total_id]["semantics"] = "total"
    card = pilot.generate_card(conn, "A", p, now)
    assert card["status"] == "increase"
    assert "25 lb per hand" in card["card_text"]
    assert "25 lb total load" in card["card_text"]
    assert all(
        item["sets"][0]["load_kg"] == round(25 * pilot.KG_PER_LB, 8)
        for item in card["decision"]["targets"].values()
    )


def test_changed_unit_or_load_semantics_breaks_comparability_fail_closed(conn):
    now = utc()
    baseline_two(conn, now)
    pilot.generate_card(conn, "A", profile(units="lb", increment=5, semantics="per_hand"), now)
    conflicting = profile(units="lb", increment=5, semantics="per_hand")
    first_id = pilot.PILOT_ROUTINES["A"]["exercise_template_ids"][0]
    conflicting["exercise_load_config"][first_id]["semantics"] = "total"
    with pytest.raises(pilot.PilotConfigurationError, match="conflicting load semantics"):
        pilot.generate_card(conn, "A", conflicting, now + timedelta(minutes=1))


def test_stale_or_missing_feedback_never_increases(conn):
    now = utc()
    latest = now - timedelta(days=2)
    add_workout(conn, "w1", latest - timedelta(days=3))
    add_workout(conn, "w2", latest)
    set_fresh(conn, now - timedelta(hours=72))
    pilot.refresh_evidence(conn, now)
    # Explicit no-pain for only one exposure must not infer the other.
    pilot.record_feedback(conn, "w1", "no", now=now)
    card = pilot.generate_card(conn, "A", profile(), now)
    assert "STALE_DATA" in card["decision"]["reason_codes"]
    assert card["decision"]["changed_exercise_template_ids"] == []

    set_fresh(conn, now)
    card = pilot.generate_card(conn, "A", profile(), now + timedelta(minutes=1))
    assert "FEEDBACK_MISSING" in card["decision"]["reason_codes"]
    assert card["decision"]["changed_exercise_template_ids"] == []

    conn.execute(
        """INSERT INTO hevy_sync_state VALUES ('coach_strength_cron_state', ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        [json.dumps({"freshness": False, "error_class": "timeout"}), pilot.utc_naive(now)],
    )
    failed_sync_card = pilot.generate_card(conn, "A", profile(), now + timedelta(minutes=2))
    assert "STALE_DATA" in failed_sync_card["decision"]["reason_codes"]
    assert failed_sync_card["decision"]["changed_exercise_template_ids"] == []


def test_naive_hevy_timestamps_are_utc_then_presented_in_los_angeles():
    before_dst = datetime(2026, 3, 8, 9, 30)  # DuckDB TIMESTAMP, semantically UTC.
    after_dst = datetime(2026, 3, 8, 10, 30)
    utc_day_to_prior_pt_day = datetime(2026, 8, 26, 2, 30)
    assert pilot.local_iso(before_dst).startswith("2026-03-08T01:30:00-08:00")
    assert pilot.local_iso(after_dst).startswith("2026-03-08T03:30:00-07:00")
    assert pilot.local_iso(utc_day_to_prior_pt_day).startswith("2026-08-25T19:30:00-07:00")
    assert pilot.utc_naive(datetime(2026, 8, 20, 5, tzinfo=timezone(timedelta(hours=-7)))) == datetime(2026, 8, 20, 12)
    with pytest.raises(pilot.PilotConfigurationError, match="explicit UTC offset"):
        pilot.explicit_timestamp("2026-08-26T02:30:00")


def test_one_shot_checkin_waits_then_never_chases(conn):
    now = utc()
    add_workout(conn, "baseline", now - timedelta(days=2))
    set_fresh(conn, now)
    pilot.refresh_evidence(conn, now)  # Existing history is deliberately suppressed.
    assert pilot.claim_checkin(conn, now)["status"] == "none"

    end = add_workout(conn, "new", now + timedelta(hours=1))
    set_fresh(conn, end)
    pilot.refresh_evidence(conn, end)
    assert pilot.claim_checkin(conn, end + timedelta(minutes=9))["status"] == "none"
    prompt = pilot.claim_checkin(conn, end + timedelta(minutes=10))
    assert prompt["status"] == "prompt"
    assert prompt["workout_id"] == "new"
    assert prompt["retry_allowed"] is False
    assert prompt["context_scope"] == pilot.LOCAL_CONTEXT
    assert pilot.claim_checkin(conn, end + timedelta(minutes=20))["status"] == "none"
    assert conn.execute(
        "SELECT state FROM coach_strength_pilot_checkins WHERE workout_id='new'"
    ).fetchone()[0] == "sent"


def test_repeated_skipped_prompts_enter_passive_mode(conn):
    now = utc()
    pilot.refresh_evidence(conn, now)
    for number, start in ((1, now + timedelta(hours=1)), (2, now + timedelta(days=1))):
        end = add_workout(conn, f"skip-{number}", start)
        pilot.refresh_evidence(conn, end)
        assert pilot.claim_checkin(conn, end + timedelta(minutes=10))["status"] == "prompt"
        after_expiry = pilot.claim_checkin(conn, end + timedelta(hours=26, minutes=1))
        if number == 1:
            assert after_expiry["status"] == "none"
        else:
            assert after_expiry == {"status": "none", "mode": "passive"}
    third_end = add_workout(conn, "passive-new", now + timedelta(days=2))
    pilot.refresh_evidence(conn, third_end)
    assert conn.execute(
        "SELECT state FROM coach_strength_pilot_checkins WHERE workout_id='passive-new'"
    ).fetchone()[0] == "suppressed_passive"
    assert pilot.claim_checkin(conn, third_end + timedelta(minutes=20)) == {
        "status": "none", "mode": "passive"
    }


def test_cached_card_survives_hevy_unavailability(conn):
    now = utc()
    baseline_two(conn, now)
    generated = pilot.generate_card(conn, "A", profile(), now)
    conn.execute("DROP TABLE hevy_sets")
    conn.execute("DROP TABLE hevy_workouts")
    conn.execute("DROP TABLE hevy_sync_state")
    conn.execute("DROP TABLE coach_routines")
    cached = pilot.cached_card(conn, "A")
    assert cached["prescription_id"] == generated["prescription_id"]
    assert cached["card_text"] == generated["card_text"]


def test_pilot_has_no_hevy_network_or_database_write_path(conn):
    now = utc()
    add_workout(conn, "read-only-source", now - timedelta(days=1))
    set_fresh(conn, now)
    before = {
        table: conn.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
        for table in ("hevy_workouts", "hevy_sets", "hevy_sync_state", "coach_routines")
    }
    pilot.refresh_evidence(conn, now)
    pilot.record_feedback(conn, "read-only-source", "no", now=now)
    pilot.generate_card(conn, "A", profile(), now)
    after = {
        table: conn.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
        for table in before
    }
    assert after == before
    tree = ast.parse(SCRIPT.read_text())
    imported_roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not ({"requests", "urllib", "http", "socket"} & imported_roots)
    source = SCRIPT.read_text().lower()
    assert not any(token in source for token in (".post(", ".put(", ".delete("))
    assert pilot.HEVY_WRITES_ENABLED is False


def test_checkin_expires_around_twenty_six_hours_without_retry_state(conn):
    now = utc()
    pilot.refresh_evidence(conn, now)  # Establish baseline before the new completion.
    end = add_workout(conn, "late", now + timedelta(hours=1))
    set_fresh(conn, end)
    pilot.refresh_evidence(conn, end)
    prompt = pilot.claim_checkin(conn, end + timedelta(hours=24))
    assert prompt["status"] == "prompt"
    assert prompt["retry_allowed"] is False
    assert conn.execute(
        "SELECT state FROM coach_strength_pilot_checkins WHERE workout_id='late'"
    ).fetchone()[0] == "sent"
    assert pilot.claim_checkin(conn, end + timedelta(hours=26, minutes=1))["status"] == "none"
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(coach_strength_pilot_checkins)").fetchall()
    }
    assert "retry_count" not in columns and "next_retry_at" not in columns
    assert conn.execute(
        "SELECT state FROM coach_strength_pilot_checkins WHERE workout_id='late'"
    ).fetchone()[0] == "skipped"
