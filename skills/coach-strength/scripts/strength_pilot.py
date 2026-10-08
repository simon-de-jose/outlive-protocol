#!/usr/bin/env python3
"""Deterministic, local-only Coach Strength A/B pilot engine.

Hevy tables are read-only evidence. This module only writes tables prefixed
``coach_strength_pilot_`` and never calls or writes to Hevy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import duckdb
from bootstrap.env import db_path

RULE_VERSION = "coach-strength-pilot-v1"
SCHEMA_VERSION = 1
PRESENTATION_TIMEZONE = "America/Los_Angeles"
LONG_GAP_DAYS = 10
CHECKIN_DELAY_MINUTES = 10
# Daily 21:00 polling needs the prompt to survive to the next run, but not much longer.
CHECKIN_EXPIRY_HOURS = 26
PASSIVE_AFTER_SKIPS = 2
HEVY_WRITES_ENABLED = False
KG_PER_LB = 0.45359237
ID_NAMESPACE = uuid.UUID("c04a483e-e90f-45c9-80b1-2db65ca8a662")
LOCAL_CONTEXT = "approved_outlive_local_only"
CRON_INTEGRATION_STATE_KEY = "hevy_sync_cron_integrated"

PILOT_ROUTINES = {
    "A": {
        "hevy_routine_id": "11dcf611-a6f3-4b13-98ed-2f916f634012",
        "title": "Outlive A — Push Emphasis",
        "exercise_template_ids": (
            "3601968B", "67280085", "B537D09F", "6AC96645",
            "72CFFAD5", "E5988A0A",
        ),
    },
    "B": {
        "hevy_routine_id": "5e558f2e-8d39-4536-bc66-34d0cc6c2cc5",
        "title": "Outlive B — Pull Emphasis",
        "exercise_template_ids": (
            "F1E57334", "B5D3A742", "07B38369", "37FCC2BB",
            "7E3BC8B6", "3765684D", "422B08F1",
        ),
    },
}


class PilotError(RuntimeError):
    """Base class for deterministic pilot failures."""


class PilotConfigurationError(PilotError):
    """Raised when required profile/routine configuration is unsafe or missing."""


class PilotDataError(PilotError):
    """Raised when local evidence cannot be interpreted safely."""


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def content_hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(kind, value):
    return f"{kind}_{uuid.uuid5(ID_NAMESPACE, kind + ':' + content_hash(value))}"


def utc_now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc_aware(value):
    """Treat naive Hevy/DuckDB TIMESTAMP values as UTC, never local time."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise PilotDataError(f"not a timestamp: {value!r}")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_naive(value):
    return as_utc_aware(value).replace(tzinfo=None)


def explicit_timestamp(value):
    """Parse operator/config timestamps only when an offset makes semantics explicit."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PilotConfigurationError("timestamp must include an explicit UTC offset")
    return value.astimezone(timezone.utc)


def local_iso(value):
    return as_utc_aware(value).astimezone(ZoneInfo(PRESENTATION_TIMEZONE)).isoformat()


def _json(value, fallback=None):
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    parsed = json.loads(value)
    return parsed


def _finite_positive(name, value):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PilotConfigurationError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(number) or number <= 0:
        raise PilotConfigurationError(f"{name} must be a positive finite number")
    return number


def init_schema(conn):
    """Apply the pilot migration idempotently without altering legacy tables."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coach_strength_pilot_schema (
            version INTEGER PRIMARY KEY,
            applied_at_utc TIMESTAMP NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coach_strength_pilot_state (
            key VARCHAR PRIMARY KEY,
            value_json VARCHAR NOT NULL,
            updated_at_utc TIMESTAMP NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coach_strength_pilot_prescriptions (
            prescription_id VARCHAR PRIMARY KEY,
            routine_key VARCHAR NOT NULL,
            routine_id VARCHAR NOT NULL,
            hevy_routine_id VARCHAR NOT NULL,
            created_at_utc TIMESTAMP NOT NULL,
            routine_snapshot_json VARCHAR NOT NULL,
            routine_hash VARCHAR NOT NULL,
            profile_snapshot_json VARCHAR NOT NULL,
            profile_hash VARCHAR NOT NULL,
            rule_version VARCHAR NOT NULL,
            evidence_hash VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            reason_codes_json VARCHAR NOT NULL,
            confidence VARCHAR NOT NULL,
            decision_json VARCHAR NOT NULL,
            card_json VARCHAR NOT NULL,
            card_text VARCHAR NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coach_strength_pilot_results (
            workout_id VARCHAR PRIMARY KEY,
            result_id VARCHAR NOT NULL UNIQUE,
            prescription_id VARCHAR,
            routine_key VARCHAR,
            routine_id VARCHAR,
            matched_at_utc TIMESTAMP NOT NULL,
            workout_start_utc TIMESTAMP NOT NULL,
            workout_end_utc TIMESTAMP NOT NULL,
            workout_updated_utc TIMESTAMP,
            routine_snapshot_json VARCHAR NOT NULL,
            routine_hash VARCHAR NOT NULL,
            rule_version VARCHAR NOT NULL,
            evidence_hash VARCHAR NOT NULL,
            match_status VARCHAR NOT NULL,
            reason_codes_json VARCHAR NOT NULL,
            result_json VARCHAR NOT NULL,
            source_status VARCHAR NOT NULL,
            deleted_at_utc TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coach_strength_pilot_feedback (
            feedback_id VARCHAR PRIMARY KEY,
            workout_id VARCHAR NOT NULL UNIQUE,
            recorded_at_utc TIMESTAMP NOT NULL,
            pain_status VARCHAR NOT NULL,
            change_requested BOOLEAN NOT NULL,
            change_note VARCHAR,
            context_scope VARCHAR NOT NULL,
            evidence_hash VARCHAR NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coach_strength_pilot_checkins (
            workout_id VARCHAR PRIMARY KEY,
            prompt_id VARCHAR NOT NULL UNIQUE,
            created_at_utc TIMESTAMP NOT NULL,
            eligible_at_utc TIMESTAMP NOT NULL,
            expires_at_utc TIMESTAMP NOT NULL,
            state VARCHAR NOT NULL,
            prompted_at_utc TIMESTAMP,
            answered_at_utc TIMESTAMP
        )
    """)
    conn.execute(
        """INSERT INTO coach_strength_pilot_schema(version, applied_at_utc)
           VALUES (?, ?) ON CONFLICT(version) DO NOTHING""",
        [SCHEMA_VERSION, utc_now_naive()],
    )


def _state_get(conn, key):
    row = conn.execute(
        "SELECT value_json FROM coach_strength_pilot_state WHERE key = ?", [key]
    ).fetchone()
    return _json(row[0]) if row else None


def _state_set(conn, key, value, now):
    payload = canonical_json(value)
    conn.execute(
        """INSERT INTO coach_strength_pilot_state(key, value_json, updated_at_utc)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
               updated_at_utc = excluded.updated_at_utc""",
        [key, payload, utc_naive(now)],
    )


def _routine_rows(conn):
    return conn.execute(
        """SELECT id, hevy_routine_id, title, exercises
           FROM coach_routines
           WHERE hevy_routine_id IN (?, ?)
           ORDER BY hevy_routine_id, id""",
        [PILOT_ROUTINES["A"]["hevy_routine_id"], PILOT_ROUTINES["B"]["hevy_routine_id"]],
    ).fetchall()


def load_routines(conn):
    """Load exactly the approved current A/B identities and fail closed on drift."""
    by_hevy = {}
    for row in _routine_rows(conn):
        by_hevy.setdefault(row[1], []).append(row)
    routines = {}
    for key, approved in PILOT_ROUTINES.items():
        rows = by_hevy.get(approved["hevy_routine_id"], [])
        if len(rows) != 1:
            raise PilotConfigurationError(
                f"pilot routine {key} must resolve to exactly one local row"
            )
        local_id, hevy_id, title, raw_exercises = rows[0]
        exercises = _json(raw_exercises, [])
        if title != approved["title"]:
            raise PilotConfigurationError(f"pilot routine {key} title changed")
        actual_ids = tuple(str(item.get("exercise_template_id")) for item in exercises)
        if actual_ids != approved["exercise_template_ids"]:
            raise PilotConfigurationError(
                f"pilot routine {key} exercise identities changed; review required"
            )
        if len(set(actual_ids)) != len(actual_ids):
            raise PilotConfigurationError(f"pilot routine {key} has duplicate exercise identities")
        normalized = {
            "routine_key": key,
            "id": str(local_id),
            "hevy_routine_id": str(hevy_id),
            "title": title,
            "exercises": exercises,
        }
        normalized["routine_hash"] = content_hash(normalized)
        routines[key] = normalized
    return routines


def confirmed_profile_defaults(profile, routine_key=None):
    """Apply only owner-confirmed pilot defaults; availability stays explicit."""
    value = dict(profile or {})
    value.setdefault("goals", ["hypertrophy", "long-term strength"])
    value.setdefault("injuries", [])
    value.setdefault("pain_triggers", [])
    value.setdefault("avoid_list", [])
    value.setdefault("units", "lb")
    configs = dict(value.get("exercise_load_config") or {})
    keys = [str(routine_key).upper()] if routine_key else sorted(PILOT_ROUTINES)
    for key in keys:
        if key not in PILOT_ROUTINES:
            raise PilotConfigurationError("routine must be A or B")
        for template_id in PILOT_ROUTINES[key]["exercise_template_ids"]:
            configs.setdefault(template_id, {"increment": 5.0, "semantics": "per_hand"})
    value["exercise_load_config"] = configs
    return value


def validate_profile(profile, routines, routine_key=None):
    """Validate the explicit pilot profile; unknown load semantics are fatal."""
    if not isinstance(profile, dict):
        raise PilotConfigurationError("profile JSON is required")
    goals = profile.get("goals")
    if not isinstance(goals, list) or not goals:
        raise PilotConfigurationError("profile.goals must be a non-empty list")
    days = profile.get("days_per_week")
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 7:
        raise PilotConfigurationError("profile.days_per_week must be an integer from 1 to 7")
    for field in ("equipment_constraints", "injuries", "pain_triggers", "avoid_list"):
        if not isinstance(profile.get(field), list):
            raise PilotConfigurationError(f"profile.{field} must be a list")
    units = profile.get("units")
    if units not in {"kg", "lb"}:
        raise PilotConfigurationError("profile.units must be exactly 'kg' or 'lb'")
    freshness = _finite_positive("profile.data_freshness_hours", profile.get("data_freshness_hours"))
    configs = profile.get("exercise_load_config")
    if not isinstance(configs, dict):
        raise PilotConfigurationError("profile.exercise_load_config is required")
    keys = [routine_key] if routine_key else sorted(routines)
    clean_configs = {}
    for key in keys:
        for template_id in PILOT_ROUTINES[key]["exercise_template_ids"]:
            config = configs.get(template_id)
            if not isinstance(config, dict):
                raise PilotConfigurationError(f"missing load config for {template_id}")
            semantics = config.get("semantics")
            if semantics not in {"per_hand", "total"}:
                raise PilotConfigurationError(
                    f"load semantics for {template_id} must be per_hand or total"
                )
            clean_configs[template_id] = {
                "increment": _finite_positive(
                    f"increment for {template_id}", config.get("increment")
                ),
                "semantics": semantics,
            }
    normalized = {
        "goals": [str(item) for item in goals],
        "days_per_week": days,
        "equipment_constraints": [str(item) for item in profile["equipment_constraints"]],
        "injuries": [str(item) for item in profile["injuries"]],
        "pain_triggers": [str(item) for item in profile["pain_triggers"]],
        "avoid_list": [str(item) for item in profile["avoid_list"]],
        "units": units,
        "data_freshness_hours": freshness,
        "exercise_load_config": clean_configs,
    }
    return normalized


def _routine_targets(routine):
    """Return all planned sets keyed by stable routine slot + movement identity."""
    result = {}
    for position, exercise in enumerate(routine["exercises"]):
        template_id = str(exercise["exercise_template_id"])
        slot_index = int(exercise.get("index", position))
        target_sets = []
        seen_set_slots = set()
        normal_count = 0
        for set_position, item in enumerate(exercise.get("sets", [])):
            set_index = int(item.get("index", set_position))
            set_type = str(item.get("type") or "normal").lower()
            set_slot = (set_index, set_type)
            if set_slot in seen_set_slots:
                raise PilotConfigurationError(
                    f"duplicate planned set slot for {template_id}; review required"
                )
            seen_set_slots.add(set_slot)
            load = item.get("weight_kg")
            reps = item.get("reps")
            if load is None or reps is None or float(load) < 0 or int(reps) <= 0:
                raise PilotConfigurationError(
                    f"unknown load/reps target for {template_id}; review required"
                )
            if set_type == "normal":
                normal_count += 1
            target_sets.append({
                "set_index": set_index, "set_type": set_type,
                "load_kg": round(float(load), 8), "reps": int(reps),
            })
        if not target_sets or normal_count == 0:
            raise PilotConfigurationError(f"no complete working targets for {template_id}")
        target_sets.sort(key=lambda item: (item["set_index"], item["set_type"]))
        result[template_id] = {
            "title": str(exercise.get("title") or template_id),
            "slot_id": f"{routine['routine_key']}:{slot_index}:{template_id}",
            "movement_id": template_id,
            "sets": target_sets,
        }
    return result


def _latest_prescription(conn, routine, before=None):
    query = """SELECT prescription_id, routine_hash, decision_json
               FROM coach_strength_pilot_prescriptions
               WHERE routine_id = ?"""
    params = [routine["id"]]
    if before is not None:
        query += " AND created_at_utc <= ?"
        params.append(utc_naive(before))
    query += " ORDER BY created_at_utc DESC, prescription_id DESC LIMIT 1"
    row = conn.execute(query, params).fetchone()
    if not row or row[1] != routine["routine_hash"]:
        return None
    return {"prescription_id": row[0], "decision": _json(row[2])}


def _target_source_for_workout(conn, routine, workout_start):
    prescription = _latest_prescription(conn, routine, before=workout_start)
    if prescription:
        return prescription["prescription_id"], prescription["decision"]["targets"]
    return None, _routine_targets(routine)


def _workout_sets(conn, workout_id):
    rows = conn.execute(
        """SELECT exercise_template_id, exercise_name, set_index, set_type,
                  weight_kg, reps, distance_meters, duration_seconds, rpe, custom_metric
           FROM hevy_sets WHERE workout_id = ?
           ORDER BY exercise_template_id NULLS LAST, set_index NULLS LAST,
                    COALESCE(set_type, 'normal'), weight_kg NULLS LAST,
                    reps NULLS LAST, exercise_name NULLS LAST""",
        [workout_id],
    ).fetchall()
    return [
        {
            "exercise_template_id": row[0], "exercise_name": row[1],
            "set_index": row[2], "set_type": row[3], "weight_kg": row[4],
            "reps": row[5], "distance_meters": row[6],
            "duration_seconds": row[7], "rpe": row[8], "custom_metric": row[9],
        }
        for row in rows
    ]


def _match_routine(workout, routines):
    workout_id, title, routine_id = str(workout[0]), workout[1], workout[2]
    by_id = [r for r in routines.values() if r["hevy_routine_id"] == routine_id]
    by_title = [r for r in routines.values() if r["title"] == title]
    if len(by_id) == 1 and (not by_title or by_title == by_id):
        return by_id[0], []
    if not by_id and len(by_title) == 1:
        return by_title[0], ["AMBIGUOUS_MATCH"]
    raise PilotDataError(f"workout {workout_id} does not uniquely match an approved routine")


def _result_payload(conn, workout, routine, initial_reasons):
    workout_id, title, _, start, end, updated = workout
    prescription_id, targets = _target_source_for_workout(conn, routine, start)
    raw_sets = _workout_sets(conn, workout_id)
    grouped = {}
    actual_slot_counts = {}
    for item in raw_sets:
        template_id = item["exercise_template_id"]
        normalized_type = str(item["set_type"] or "normal").lower()
        normalized = dict(item)
        normalized["set_type"] = normalized_type
        grouped.setdefault(template_id, []).append(normalized)
        set_slot = (template_id, item["set_index"], normalized_type)
        actual_slot_counts[set_slot] = actual_slot_counts.get(set_slot, 0) + 1
    expected_ids = set(targets)
    actual_ids = {
        str(item["exercise_template_id"])
        for item in raw_sets if item["exercise_template_id"] is not None
    }
    reasons = list(initial_reasons)
    if actual_ids - expected_ids:
        reasons.append("MATERIAL_CHANGE")
    ambiguous_templates = {
        str(template_id)
        for (template_id, _, _), count in actual_slot_counts.items()
        if template_id is not None and count > 1
    }
    if ambiguous_templates:
        reasons.append("AMBIGUOUS_MATCH")
    exercises = {}
    for template_id, target in targets.items():
        actual = grouped.get(template_id, [])
        actual.sort(key=lambda item: (
            item["set_index"] if item["set_index"] is not None else 10**9,
            item["set_type"], item["weight_kg"] if item["weight_kg"] is not None else math.inf,
            item["reps"] if item["reps"] is not None else math.inf,
        ))
        exercise_reasons = []
        status = "achieved"
        if template_id in ambiguous_templates:
            status = "review"
            exercise_reasons.append("AMBIGUOUS_MATCH")
        elif len(actual) != len(target["sets"]):
            status = "incomplete"
            exercise_reasons.append("INCOMPLETE_SESSION")
        else:
            for actual_set, target_set in zip(actual, target["sets"]):
                if actual_set["weight_kg"] is None or actual_set["reps"] is None:
                    status = "incomplete"
                    exercise_reasons.append("INCOMPLETE_SESSION")
                    break
                if (
                    actual_set["set_index"] != target_set["set_index"]
                    or actual_set["set_type"] != target_set["set_type"]
                ):
                    status = "review"
                    exercise_reasons.append("AMBIGUOUS_MATCH")
                    break
                if (
                    float(actual_set["weight_kg"]) + 0.02 < float(target_set["load_kg"])
                    or int(actual_set["reps"]) < int(target_set["reps"])
                ):
                    status = "missed"
                    exercise_reasons.append("MISSED_REPS")
                    break
        exercises[template_id] = {
            "title": target["title"],
            "slot_id": target["slot_id"],
            "movement_id": target["movement_id"],
            "load_semantics": target.get("load_semantics"),
            "display_unit": target.get("display_unit"),
            "target_sets": target["sets"],
            "actual_sets": [
                {
                    "set_index": item["set_index"], "set_type": item["set_type"],
                    "load_kg": item["weight_kg"], "reps": item["reps"],
                }
                for item in actual
            ],
            "status": status,
            "reason_codes": sorted(set(exercise_reasons)),
        }
        reasons.extend(exercise_reasons)
    reasons = sorted(set(reasons))
    match_status = "review" if {"AMBIGUOUS_MATCH", "MATERIAL_CHANGE"} & set(reasons) else "matched"
    payload = {
        "workout_id": str(workout_id),
        "title": title,
        "routine_key": routine["routine_key"],
        "routine_id": routine["id"],
        "prescription_id": prescription_id,
        "workout_start_utc": as_utc_aware(start).isoformat(),
        "workout_end_utc": as_utc_aware(end).isoformat(),
        "workout_updated_utc": as_utc_aware(updated).isoformat() if updated else None,
        "exercises": exercises,
        "raw_sets": raw_sets,
        "reason_codes": reasons,
        "match_status": match_status,
        "rule_version": RULE_VERSION,
    }
    return payload


def refresh_evidence(conn, now=None):
    """Reconcile completed A/B workouts, preserving edit/delete and prompt identity."""
    init_schema(conn)
    now = as_utc_aware(now or datetime.now(timezone.utc))
    routines = load_routines(conn)
    approved_ids = [r["hevy_routine_id"] for r in routines.values()]
    approved_titles = [r["title"] for r in routines.values()]
    rows = conn.execute(
        """SELECT id, title, routine_id, start_time, end_time, updated_at
           FROM hevy_workouts
           WHERE end_time IS NOT NULL
             AND (routine_id IN (?, ?) OR title IN (?, ?))
           ORDER BY start_time, id""",
        approved_ids + approved_titles,
    ).fetchall()
    baseline = _state_get(conn, "evidence_baselined") is None
    checkin_mode = _state_get(conn, "checkin_mode") or {"mode": "active"}
    seen = set()
    inserted = updated_count = unchanged = 0
    for workout in rows:
        workout_id = str(workout[0])
        seen.add(workout_id)
        try:
            routine, match_reasons = _match_routine(workout, routines)
            payload = _result_payload(conn, workout, routine, match_reasons)
        except PilotDataError:
            continue
        evidence_hash = content_hash(payload)
        existing = conn.execute(
            "SELECT evidence_hash, source_status FROM coach_strength_pilot_results WHERE workout_id = ?",
            [workout_id],
        ).fetchone()
        if existing and existing == (evidence_hash, "active"):
            unchanged += 1
            continue
        routine_snapshot = {
            key: routine[key]
            for key in ("routine_key", "id", "hevy_routine_id", "title", "exercises")
        }
        values = [
            workout_id, stable_id("result", {"workout_id": workout_id}),
            payload["prescription_id"], routine["routine_key"], routine["id"],
            utc_naive(now), utc_naive(workout[3]), utc_naive(workout[4]),
            utc_naive(workout[5]) if workout[5] else None,
            canonical_json(routine_snapshot), routine["routine_hash"], RULE_VERSION,
            evidence_hash, payload["match_status"], canonical_json(payload["reason_codes"]),
            canonical_json(payload), "active", None,
        ]
        conn.execute(
            """INSERT INTO coach_strength_pilot_results(
                   workout_id, result_id, prescription_id, routine_key, routine_id,
                   matched_at_utc, workout_start_utc, workout_end_utc, workout_updated_utc,
                   routine_snapshot_json, routine_hash, rule_version, evidence_hash,
                   match_status, reason_codes_json, result_json, source_status, deleted_at_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(workout_id) DO UPDATE SET
                   prescription_id=excluded.prescription_id,
                   routine_key=excluded.routine_key, routine_id=excluded.routine_id,
                   matched_at_utc=excluded.matched_at_utc,
                   workout_start_utc=excluded.workout_start_utc,
                   workout_end_utc=excluded.workout_end_utc,
                   workout_updated_utc=excluded.workout_updated_utc,
                   routine_snapshot_json=excluded.routine_snapshot_json,
                   routine_hash=excluded.routine_hash, rule_version=excluded.rule_version,
                   evidence_hash=excluded.evidence_hash,
                   match_status=excluded.match_status,
                   reason_codes_json=excluded.reason_codes_json,
                   result_json=excluded.result_json, source_status='active', deleted_at_utc=NULL""",
            values,
        )
        if existing:
            updated_count += 1
        else:
            inserted += 1
            state = (
                "suppressed_baseline" if baseline
                else "suppressed_passive" if checkin_mode.get("mode") == "passive"
                else "pending"
            )
            conn.execute(
                """INSERT INTO coach_strength_pilot_checkins(
                       workout_id, prompt_id, created_at_utc, eligible_at_utc,
                       expires_at_utc, state, prompted_at_utc, answered_at_utc)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                   ON CONFLICT(workout_id) DO NOTHING""",
                [
                    workout_id, stable_id("checkin", {"workout_id": workout_id}), utc_naive(now),
                    utc_naive(as_utc_aware(workout[4]) + timedelta(minutes=CHECKIN_DELAY_MINUTES)),
                    utc_naive(as_utc_aware(workout[4]) + timedelta(hours=CHECKIN_EXPIRY_HOURS)),
                    state,
                ],
            )
    deleted = 0
    active_rows = conn.execute(
        """SELECT workout_id, evidence_hash FROM coach_strength_pilot_results
           WHERE source_status = 'active'"""
    ).fetchall()
    for workout_id, prior_hash in active_rows:
        if workout_id not in seen:
            deleted_hash = content_hash({
                "prior_evidence_hash": prior_hash,
                "source_status": "deleted",
                "reason_codes": ["SOURCE_DELETED"],
            })
            conn.execute(
                """UPDATE coach_strength_pilot_results
                   SET source_status='deleted', match_status='review', deleted_at_utc=?,
                       evidence_hash=?, reason_codes_json=? WHERE workout_id=?""",
                [utc_naive(now), deleted_hash, canonical_json(["SOURCE_DELETED"]), workout_id],
            )
            deleted += 1
    if baseline:
        _state_set(conn, "evidence_baselined", {"at_utc": now.isoformat()}, now)
    return {
        "status": "ok", "baseline": baseline, "inserted": inserted,
        "updated": updated_count, "unchanged": unchanged, "deleted": deleted,
    }


def record_feedback(conn, workout_id, pain_status, change_requested=False, change_note=None, now=None):
    """Store explicit feedback locally; absence/unknown never becomes no-pain."""
    init_schema(conn)
    if pain_status not in {"yes", "no", "unknown"}:
        raise PilotConfigurationError("pain_status must be yes, no, or unknown")
    row = conn.execute(
        "SELECT 1 FROM coach_strength_pilot_results WHERE workout_id = ?", [workout_id]
    ).fetchone()
    if not row:
        raise PilotDataError("workout has not been reconciled")
    now = as_utc_aware(now or datetime.now(timezone.utc))
    payload = {
        "workout_id": str(workout_id), "pain_status": pain_status,
        "change_requested": bool(change_requested),
        "change_note": str(change_note).strip() if change_note else None,
        "context_scope": LOCAL_CONTEXT,
    }
    feedback_id = stable_id("feedback", {"workout_id": str(workout_id)})
    conn.execute(
        """INSERT INTO coach_strength_pilot_feedback(
               feedback_id, workout_id, recorded_at_utc, pain_status, change_requested,
               change_note, context_scope, evidence_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(workout_id) DO UPDATE SET
               recorded_at_utc=excluded.recorded_at_utc,
               pain_status=excluded.pain_status,
               change_requested=excluded.change_requested,
               change_note=excluded.change_note,
               context_scope=excluded.context_scope,
               evidence_hash=excluded.evidence_hash""",
        [feedback_id, workout_id, utc_naive(now), pain_status, bool(change_requested),
         payload["change_note"], LOCAL_CONTEXT, content_hash(payload)],
    )
    conn.execute(
        """UPDATE coach_strength_pilot_checkins
           SET state='answered', answered_at_utc=?
           WHERE workout_id=? AND state IN (
               'pending','sent','skipped','suppressed_passive'
           )""",
        [utc_naive(now), workout_id],
    )
    skipped_total = conn.execute(
        "SELECT COUNT(*) FROM coach_strength_pilot_checkins WHERE state='skipped'"
    ).fetchone()[0]
    _state_set(
        conn, "checkin_mode",
        {
            "mode": "active", "reset_at_utc": now.isoformat(),
            "skip_baseline": skipped_total,
        },
        now,
    )
    # Do not echo sensitive pain/change details to stdout or caller logs.
    return {
        "status": "recorded", "feedback_id": feedback_id,
        "workout_id": str(workout_id), "context_scope": LOCAL_CONTEXT,
    }


def _parse_sync_time(conn):
    row = conn.execute(
        "SELECT value FROM hevy_sync_state WHERE key = 'last_sync'"
    ).fetchone()
    if not row:
        return None
    try:
        return explicit_timestamp(row[0])
    except (ValueError, PilotDataError, PilotConfigurationError):
        return None


def _wrapper_freshness(conn):
    """Honor the reliable sync wrapper's latest success/failure signal when present."""
    row = conn.execute(
        "SELECT value FROM hevy_sync_state WHERE key = 'coach_strength_cron_state'"
    ).fetchone()
    if not row:
        return None
    try:
        state = _json(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return state.get("freshness") is True and state.get("error_class") is None


def _feedback_by_workout(conn, workout_ids):
    if not workout_ids:
        return {}
    placeholders = ",".join("?" for _ in workout_ids)
    rows = conn.execute(
        f"""SELECT workout_id, pain_status, change_requested, evidence_hash
            FROM coach_strength_pilot_feedback WHERE workout_id IN ({placeholders})""",
        workout_ids,
    ).fetchall()
    return {
        row[0]: {"pain_status": row[1], "change_requested": bool(row[2]), "evidence_hash": row[3]}
        for row in rows
    }


def _active_results(conn, routine):
    rows = conn.execute(
        """SELECT workout_id, workout_start_utc, evidence_hash, match_status,
                  reason_codes_json, result_json
           FROM coach_strength_pilot_results
           WHERE routine_id=? AND source_status='active'
           ORDER BY workout_start_utc DESC, workout_id DESC""",
        [routine["id"]],
    ).fetchall()
    return [
        {
            "workout_id": row[0], "start": as_utc_aware(row[1]), "evidence_hash": row[2],
            "match_status": row[3], "reason_codes": _json(row[4], []),
            "result": _json(row[5]),
        }
        for row in rows
    ]


def _display_load(kg, units):
    value = float(kg) if units == "kg" else float(kg) / KG_PER_LB
    rounded = round(value, 2)
    if abs(rounded - round(rounded)) < 0.005:
        return str(int(round(rounded)))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _increment_kg(increment, units):
    return increment if units == "kg" else increment * KG_PER_LB


def _apply_target_context(targets, profile):
    contextual = json.loads(canonical_json(targets))
    for template_id, target in contextual.items():
        expected_semantics = profile["exercise_load_config"][template_id]["semantics"]
        existing_semantics = target.get("load_semantics")
        existing_unit = target.get("display_unit")
        if existing_semantics not in {None, expected_semantics}:
            raise PilotConfigurationError(
                f"conflicting load semantics for {template_id}; review required"
            )
        if existing_unit not in {None, profile["units"]}:
            raise PilotConfigurationError(
                f"conflicting display units for {template_id}; review required"
            )
        target["load_semantics"] = expected_semantics
        target["display_unit"] = profile["units"]
    return contextual


def _target_signature(exercise_result, current_target):
    semantics = exercise_result.get("load_semantics") or current_target["load_semantics"]
    unit = exercise_result.get("display_unit") or current_target["display_unit"]
    return content_hash({
        "slot_id": exercise_result["slot_id"],
        "movement_id": exercise_result["movement_id"],
        "load_semantics": semantics,
        "display_unit": unit,
        "target_sets": exercise_result["target_sets"],
    })


def _decision(conn, routine, profile, now):
    routine_targets = _apply_target_context(_routine_targets(routine), profile)
    previous = _latest_prescription(conn, routine)
    previous_consumed = False
    if previous:
        previous_consumed = conn.execute(
            """SELECT 1 FROM coach_strength_pilot_results
               WHERE prescription_id=? AND source_status='active' LIMIT 1""",
            [previous["prescription_id"]],
        ).fetchone() is not None
    if previous and previous_consumed:
        base_targets = previous["decision"]["targets"]
    elif previous:
        base_targets = previous["decision"].get("base_targets", routine_targets)
    else:
        base_targets = routine_targets
    # Defensive copies through canonical JSON prevent mutation of source snapshots.
    base_targets = _apply_target_context(base_targets, profile)
    targets = json.loads(canonical_json(base_targets))
    results = _active_results(conn, routine)
    feedback = _feedback_by_workout(conn, [item["workout_id"] for item in results[:2]])
    global_reasons = []
    if profile["injuries"] or profile["pain_triggers"] or profile["avoid_list"]:
        global_reasons.append("PROFILE_LIMITATION_REVIEW")
    sync_time = _parse_sync_time(conn)
    wrapper_freshness = _wrapper_freshness(conn)
    if (
        sync_time is None
        or now - sync_time > timedelta(hours=profile["data_freshness_hours"])
        or wrapper_freshness is False
    ):
        global_reasons.append("STALE_DATA")
    deleted_rows = conn.execute(
        """SELECT evidence_hash FROM coach_strength_pilot_results
           WHERE routine_id=? AND source_status='deleted'
           ORDER BY workout_start_utc DESC, workout_id DESC""",
        [routine["id"]],
    ).fetchall()
    deleted_hashes = [row[0] for row in deleted_rows]
    if deleted_hashes:
        global_reasons.append("SOURCE_DELETED")
    if results:
        if now - results[0]["start"] > timedelta(days=LONG_GAP_DAYS):
            global_reasons.append("LONG_GAP")
        if len(results) >= 2 and results[0]["start"] - results[1]["start"] > timedelta(days=LONG_GAP_DAYS):
            global_reasons.append("LONG_GAP")
        for item in results[:2]:
            global_reasons.extend(
                reason for reason in item["reason_codes"]
                if reason in {
                    "AMBIGUOUS_MATCH", "MATERIAL_CHANGE", "MISSED_REPS",
                    "INCOMPLETE_SESSION",
                }
            )
            fb = feedback.get(item["workout_id"])
            if fb and fb["pain_status"] == "yes":
                global_reasons.append("PAIN_REPORTED")
            if fb and fb["change_requested"]:
                global_reasons.append("CHANGE_REQUESTED")
    global_reasons = sorted(set(global_reasons))
    hard_block = bool(global_reasons)
    changed = []
    all_reasons = set(global_reasons)
    exercise_decisions = {}
    for template_id, target in targets.items():
        reasons = list(global_reasons)
        action = "hold"
        relevant = [item for item in results if template_id in item["result"]["exercises"]][:2]
        if not hard_block:
            if len(relevant) < 2:
                reasons.append("INSUFFICIENT_EVIDENCE")
            else:
                latest_two_feedback = [feedback.get(item["workout_id"]) for item in relevant]
                if any(item is None or item["pain_status"] != "no" for item in latest_two_feedback):
                    reasons.append("FEEDBACK_MISSING")
                statuses = [item["result"]["exercises"][template_id]["status"] for item in relevant]
                if "missed" in statuses:
                    reasons.append("MISSED_REPS")
                if "incomplete" in statuses:
                    reasons.append("INCOMPLETE_SESSION")
                if "review" in statuses:
                    reasons.append("AMBIGUOUS_MATCH")
                signatures = [
                    _target_signature(
                        item["result"]["exercises"][template_id], target
                    )
                    for item in relevant
                ]
                current_signature = content_hash({
                    "slot_id": target["slot_id"],
                    "movement_id": target["movement_id"],
                    "load_semantics": target["load_semantics"],
                    "display_unit": target["display_unit"],
                    "target_sets": target["sets"],
                })
                if len(set(signatures + [current_signature])) != 1:
                    reasons.append("NONCOMPARABLE_EXPOSURES")
                if not reasons and statuses == ["achieved", "achieved"]:
                    increment = profile["exercise_load_config"][template_id]["increment"]
                    increment_kg = _increment_kg(increment, profile["units"])
                    for target_set in target["sets"]:
                        if target_set["set_type"] == "normal":
                            target_set["load_kg"] = round(
                                float(target_set["load_kg"]) + increment_kg, 8
                            )
                    action = "increase"
                    reasons.append("TWO_COMPARABLE_EXPOSURES_ACHIEVED")
                    changed.append(template_id)
        if not reasons:
            reasons.append("HOLD")
        reasons = sorted(set(reasons))
        all_reasons.update(reasons)
        exercise_decisions[template_id] = {
            "action": action,
            "reason_codes": reasons,
            "confidence": "high" if action == "increase" else ("low" if hard_block else "medium"),
        }
    review_reasons = {
        "PAIN_REPORTED", "CHANGE_REQUESTED", "AMBIGUOUS_MATCH",
        "MATERIAL_CHANGE", "STALE_DATA", "LONG_GAP", "SOURCE_DELETED",
        "PROFILE_LIMITATION_REVIEW",
    }
    status = "increase" if changed else (
        "review" if review_reasons & set(global_reasons) else "hold"
    )
    explicit_no_pain = (
        len(results) >= 2
        and all(
            feedback.get(item["workout_id"], {}).get("pain_status") == "no"
            for item in results[:2]
        )
    )
    confidence_dimensions = {
        "identity_match": "low" if {"AMBIGUOUS_MATCH", "MATERIAL_CHANGE", "SOURCE_DELETED"} & set(global_reasons) else "high",
        "data_freshness": "low" if "STALE_DATA" in global_reasons else "high",
        "completion": "low" if {"MISSED_REPS", "INCOMPLETE_SESSION"} & set(global_reasons) else ("high" if len(results) >= 2 else "unknown"),
        "feedback": "low" if "PAIN_REPORTED" in global_reasons else ("high" if explicit_no_pain else "unknown"),
        "comparability": "high" if changed else ("low" if hard_block else "medium"),
    }
    return {
        "status": status,
        "base_targets": base_targets,
        "targets": targets,
        "exercise_decisions": exercise_decisions,
        "changed_exercise_template_ids": changed,
        "reason_codes": sorted(all_reasons),
        "confidence": "high" if changed and not hard_block else ("low" if hard_block else "medium"),
        "confidence_dimensions": confidence_dimensions,
        "needs_input": bool(
            {"INSUFFICIENT_EVIDENCE", "FEEDBACK_MISSING", "PROFILE_LIMITATION_REVIEW"}
            & all_reasons
        ),
        "ask": "Complete comparable sessions and answer the one-shot safety check-in before progression.",
        "fallback": "Use the prior comfortable load; stop progression and report pain or material changes.",
        "latest_workout_local": local_iso(results[0]["start"]) if results else None,
        "source_result_hashes": [item["evidence_hash"] for item in results[:2]] + deleted_hashes,
        "source_feedback_hashes": sorted(
            item["evidence_hash"] for item in feedback.values()
        ),
        "sync_succeeded_at_utc": sync_time.isoformat() if sync_time else None,
        "rule_version": RULE_VERSION,
    }


def _card(routine, profile, decision):
    semantics_text = {"per_hand": "per hand", "total": "total load"}
    lines = [f"{routine['title']} — next session"]
    card_exercises = []
    for exercise in routine["exercises"]:
        template_id = str(exercise["exercise_template_id"])
        target = decision["targets"][template_id]
        config = profile["exercise_load_config"][template_id]
        grouped = []
        for item in target["sets"]:
            set_label = "warm-up " if item["set_type"] == "warmup" else ""
            label = (
                f"{set_label}{_display_load(item['load_kg'], profile['units'])} "
                f"{profile['units']} {semantics_text[config['semantics']]} × {item['reps']}"
            )
            if grouped and grouped[-1]["label"] == label:
                grouped[-1]["sets"] += 1
            else:
                grouped.append({"label": label, "sets": 1})
        target_text = ", ".join(f"{item['sets']}× {item['label']}" for item in grouped)
        exercise_decision = decision["exercise_decisions"][template_id]
        marker = "↑ " if exercise_decision["action"] == "increase" else ""
        lines.append(f"{marker}{target['title']}: {target_text}")
        card_exercises.append({
            "exercise_template_id": template_id,
            "title": target["title"], "target_text": target_text,
            "changed": exercise_decision["action"] == "increase",
            "load_semantics": config["semantics"],
            **exercise_decision,
        })
    short_reason = {
        "increase": "Two comparable, explicitly pain-free completed exposures.",
        "hold": "Holding until the evidence is sufficient and comparable.",
        "review": "Safety review required; no automatic progression.",
    }[decision["status"]]
    lines.append(f"Reason: {short_reason}")
    lines.append(f"Confidence: {decision['confidence']}. Fallback: {decision['fallback']}")
    card = {
        "routine_key": routine["routine_key"], "routine_title": routine["title"],
        "status": decision["status"], "exercises": card_exercises,
        "reason": short_reason, "reason_codes": decision["reason_codes"],
        "confidence": decision["confidence"],
        "confidence_dimensions": decision["confidence_dimensions"],
        "needs_input": decision["needs_input"], "ask": decision["ask"],
        "fallback": decision["fallback"],
        "presentation_timezone": PRESENTATION_TIMEZONE,
        "context_scope": LOCAL_CONTEXT,
        "hevy_writes_enabled": HEVY_WRITES_ENABLED,
    }
    return card, "\n".join(lines)


def generate_card(conn, routine_key, profile, now=None):
    """Generate and cache one content-addressed next-session prescription/card."""
    init_schema(conn)
    routine_key = str(routine_key).upper()
    if routine_key not in PILOT_ROUTINES:
        raise PilotConfigurationError("routine must be A or B")
    routines = load_routines(conn)
    profile = validate_profile(profile, routines, routine_key)
    routine = routines[routine_key]
    now = as_utc_aware(now or datetime.now(timezone.utc))
    decision = _decision(conn, routine, profile, now)
    card, card_text = _card(routine, profile, decision)
    routine_snapshot = {key: routine[key] for key in ("routine_key", "id", "hevy_routine_id", "title", "exercises")}
    profile_hash = content_hash(profile)
    evidence = {
        "routine_hash": routine["routine_hash"], "profile_hash": profile_hash,
        "rule_version": RULE_VERSION,
        "decision_status": decision["status"],
        "reason_codes": decision["reason_codes"],
        "targets": decision["targets"],
        "source_result_hashes": decision["source_result_hashes"],
        "source_feedback_hashes": decision["source_feedback_hashes"],
        "sync_succeeded_at_utc": decision["sync_succeeded_at_utc"],
    }
    evidence_hash = content_hash(evidence)
    prescription_id = stable_id("prescription", evidence)
    conn.execute(
        """INSERT INTO coach_strength_pilot_prescriptions(
               prescription_id, routine_key, routine_id, hevy_routine_id, created_at_utc,
               routine_snapshot_json, routine_hash, profile_snapshot_json, profile_hash,
               rule_version, evidence_hash, status, reason_codes_json, confidence,
               decision_json, card_json, card_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(prescription_id) DO NOTHING""",
        [
            prescription_id, routine_key, routine["id"], routine["hevy_routine_id"],
            utc_naive(now), canonical_json(routine_snapshot), routine["routine_hash"],
            canonical_json(profile), profile_hash, RULE_VERSION, evidence_hash,
            decision["status"], canonical_json(decision["reason_codes"]),
            decision["confidence"], canonical_json(decision), canonical_json(card), card_text,
        ],
    )
    return {
        "prescription_id": prescription_id, "evidence_hash": evidence_hash,
        "status": decision["status"], "decision": decision,
        "card": card, "card_text": card_text,
    }


def cached_card(conn, routine_key):
    init_schema(conn)
    row = conn.execute(
        """SELECT prescription_id, created_at_utc, status, evidence_hash, card_json, card_text
           FROM coach_strength_pilot_prescriptions WHERE routine_key=?
           ORDER BY created_at_utc DESC, prescription_id DESC LIMIT 1""",
        [str(routine_key).upper()],
    ).fetchone()
    if not row:
        return None
    return {
        "prescription_id": row[0], "created_at_utc": as_utc_aware(row[1]).isoformat(),
        "created_at_local": local_iso(row[1]), "status": row[2], "evidence_hash": row[3],
        "card": _json(row[4]), "card_text": row[5],
    }


def next_routine_key(conn):
    """Alternate from the latest active completed approved A/B workout."""
    init_schema(conn)
    row = conn.execute(
        """SELECT routine_key
           FROM coach_strength_pilot_results
           WHERE source_status='active' AND routine_key IN ('A', 'B')
           ORDER BY workout_end_utc DESC, workout_start_utc DESC, workout_id DESC
           LIMIT 1"""
    ).fetchone()
    if not row:
        return "A"
    return "B" if row[0] == "A" else "A"


def baseline_cron_integration(conn, now=None):
    """Silence pre-integration history once and mark the cron integration ready."""
    init_schema(conn)
    if _state_get(conn, CRON_INTEGRATION_STATE_KEY) is not None:
        return False
    now = as_utc_aware(now or datetime.now(timezone.utc))
    conn.execute(
        """UPDATE coach_strength_pilot_checkins
           SET state='suppressed_baseline'
           WHERE state='pending'"""
    )
    _state_set(conn, CRON_INTEGRATION_STATE_KEY, {"at_utc": now.isoformat()}, now)
    return True


def claim_checkin(conn, now=None):
    """Return one prompt for the next daily run that can see it, never retry it, and go passive after repeated skips."""
    init_schema(conn)
    now = as_utc_aware(now or datetime.now(timezone.utc))
    now_naive = utc_naive(now)
    conn.execute(
        """UPDATE coach_strength_pilot_checkins SET state='expired'
           WHERE state='pending' AND expires_at_utc < ?""",
        [now_naive],
    )
    conn.execute(
        """UPDATE coach_strength_pilot_checkins SET state='skipped'
           WHERE state='sent' AND expires_at_utc < ?""",
        [now_naive],
    )
    skipped = conn.execute(
        "SELECT COUNT(*) FROM coach_strength_pilot_checkins WHERE state='skipped'"
    ).fetchone()[0]
    mode = _state_get(conn, "checkin_mode") or {"mode": "active", "skip_baseline": 0}
    skipped_since_reset = max(0, skipped - int(mode.get("skip_baseline", 0)))
    if skipped_since_reset >= PASSIVE_AFTER_SKIPS and mode.get("mode") != "passive":
        mode = {
            "mode": "passive", "entered_at_utc": now.isoformat(),
            "skipped_checkins": skipped_since_reset,
            "skip_baseline": int(mode.get("skip_baseline", 0)),
        }
        _state_set(conn, "checkin_mode", mode, now)
        conn.execute(
            """UPDATE coach_strength_pilot_checkins SET state='suppressed_passive'
               WHERE state='pending'"""
        )
    if mode.get("mode") == "passive":
        return {"status": "none", "mode": "passive"}
    row = conn.execute(
        """SELECT c.workout_id, c.prompt_id, r.workout_end_utc, r.result_json
           FROM coach_strength_pilot_checkins c
           JOIN coach_strength_pilot_results r USING(workout_id)
           WHERE c.state='pending' AND c.eligible_at_utc <= ? AND c.expires_at_utc >= ?
             AND r.source_status='active'
           ORDER BY c.eligible_at_utc, c.workout_id LIMIT 1""",
        [now_naive, now_naive],
    ).fetchone()
    if not row:
        return {"status": "none"}
    workout_id, prompt_id, workout_end, result_json = row
    result = _json(result_json)
    updated = conn.execute(
        """UPDATE coach_strength_pilot_checkins
           SET state='sent', prompted_at_utc=?
           WHERE workout_id=? AND state='pending'
           RETURNING workout_id""",
        [now_naive, workout_id],
    ).fetchone()
    if not updated:
        return {"status": "none"}
    prompt = "Quick check: any pain or anything you want changed next time? Reply keep/change, or skip."
    return {
        "status": "prompt", "prompt_id": prompt_id, "workout_id": workout_id,
        "routine_key": result["routine_key"], "workout_end_local": local_iso(workout_end),
        "prompt": prompt, "context_scope": LOCAL_CONTEXT, "retry_allowed": False,
    }


def _load_profile(path):
    if not path:
        raise PilotConfigurationError("--profile JSON file is required")
    return json.loads(Path(path).read_text())


def _parse_as_of(value):
    return explicit_timestamp(value) if value else datetime.now(timezone.utc)


def _connect(path):
    return duckdb.connect(str(Path(path).expanduser()))


def _emit(payload):
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Deterministic local Coach Strength A/B pilot")
    parser.add_argument("--db", default=str(db_path()), help="DuckDB path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Apply idempotent local pilot migrations")
    refresh = sub.add_parser("refresh", help="Reconcile completed A/B workout evidence")
    refresh.add_argument("--as-of")
    card = sub.add_parser("card", help="Generate/cache a next-session card")
    card.add_argument("--routine", choices=["A", "B", "a", "b"], required=True)
    card.add_argument("--profile", required=True)
    card.add_argument(
        "--apply-confirmed-defaults", action="store_true",
        help="Fill only owner-confirmed goal/pain/unit/load defaults",
    )
    card.add_argument("--as-of")
    show = sub.add_parser("show-card", help="Return the latest cached card")
    show.add_argument("--routine", choices=["A", "B", "a", "b"], required=True)
    checkin = sub.add_parser("check-in", help="Claim at most one eligible one-shot check-in")
    checkin.add_argument("--as-of")
    feedback = sub.add_parser("feedback", help="Record explicit local-only post-workout feedback")
    feedback.add_argument("--workout-id", required=True)
    feedback.add_argument("--pain", choices=["yes", "no", "unknown"], required=True)
    feedback.add_argument("--change", action="store_true")
    feedback.add_argument("--change-note")
    feedback.add_argument("--as-of")
    args = parser.parse_args(argv)
    conn = _connect(args.db)
    try:
        if args.command == "init":
            init_schema(conn)
            payload = {"status": "ok", "schema_version": SCHEMA_VERSION, "hevy_writes_enabled": False}
        elif args.command == "refresh":
            payload = refresh_evidence(conn, _parse_as_of(args.as_of))
        elif args.command == "card":
            profile = _load_profile(args.profile)
            if args.apply_confirmed_defaults:
                profile = confirmed_profile_defaults(profile, args.routine)
            payload = generate_card(
                conn, args.routine, profile, _parse_as_of(args.as_of)
            )
        elif args.command == "show-card":
            payload = cached_card(conn, args.routine) or {"status": "none"}
        elif args.command == "check-in":
            payload = claim_checkin(conn, _parse_as_of(args.as_of))
        else:
            payload = record_feedback(
                conn, args.workout_id, args.pain, args.change, args.change_note,
                _parse_as_of(args.as_of),
            )
        conn.commit()
        _emit(payload)
        return 0
    except (PilotError, json.JSONDecodeError, OSError, duckdb.Error) as exc:
        conn.rollback()
        _emit({"status": "error", "error_class": type(exc).__name__, "error": str(exc)})
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
