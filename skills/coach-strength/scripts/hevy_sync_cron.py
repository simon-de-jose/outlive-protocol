#!/usr/bin/env python3
"""Quiet cron gate for Hevy sync; advisory locks are released by the kernel."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import json
import os
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import duckdb
import sync_hevy
import strength_pilot

STATE_KEY = "coach_strength_cron_state"
LOCK_PATH = Path(os.environ.get("HEVY_SYNC_LOCK", str(sync_hevy.DB_PATH) + ".lock"))
PILOT_ENABLED_ENV = "STRENGTH_PILOT_ENABLED"
PILOT_PROFILE_ENV = "STRENGTH_PILOT_PROFILE"


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("cursor is missing")
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("cursor must include UTC offset")
    return dt.astimezone(timezone.utc)


def _pilot_enabled():
    value = os.environ.get(PILOT_ENABLED_ENV, "").strip().lower()
    if value in {"", "0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    raise strength_pilot.PilotConfigurationError(
        f"{PILOT_ENABLED_ENV} must be true or false"
    )


def _pilot_profile():
    raw_path = os.environ.get(PILOT_PROFILE_ENV, "").strip()
    if not raw_path:
        raise strength_pilot.PilotConfigurationError(
            f"{PILOT_PROFILE_ENV} must point to a local JSON file"
        )
    path = Path(raw_path).expanduser()
    try:
        if not path.is_file():
            raise OSError
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise strength_pilot.PilotConfigurationError(
            f"{PILOT_PROFILE_ENV} must point to a valid local JSON file"
        ) from exc
    if not isinstance(profile, dict):
        raise strength_pilot.PilotConfigurationError(
            f"{PILOT_PROFILE_ENV} must contain a JSON object"
        )
    return profile


class LockBusy(RuntimeError):
    pass


class FreshnessError(RuntimeError):
    pass


class RunLock:
    def __init__(self, path):
        self.path = Path(path)
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            os.close(self.fd)
            self.fd = None
            raise LockBusy(f"sync lock is held: {self.path}") from exc
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)


def _state(conn):
    row = conn.execute(
        "SELECT value FROM hevy_sync_state WHERE key = ?", [STATE_KEY]
    ).fetchone()
    if not row:
        return None
    value = json.loads(row[0])
    if not isinstance(value, dict):
        raise ValueError("invalid wrapper state")
    return value


def _save(conn, state):
    sync_hevy.set_sync_state(conn, STATE_KEY, json.dumps(state, sort_keys=True))


def _classify(exc):
    if isinstance(exc, LockBusy):
        return "overlap"
    if isinstance(exc, FreshnessError):
        return "freshness"
    if isinstance(exc, sync_hevy.HevyConfigurationError):
        return "configuration"
    if isinstance(exc, strength_pilot.PilotConfigurationError):
        return "configuration"
    if isinstance(exc, strength_pilot.PilotDataError):
        return "pilot_data"
    if isinstance(exc, sync_hevy.HevyTimeoutError) or isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, sync_hevy.HevyConnectionError):
        return "connection"
    if isinstance(exc, duckdb.Error):
        return "database"
    text = str(exc).lower()
    name = type(exc).__name__.lower()
    if "timeout" in name or "deadline" in text or "timed out" in text:
        return "timeout"
    if "connection" in name or "connection" in text:
        return "connection"
    if "429" in text or "rate" in text:
        return "rate_limit"
    return "api" if "http" in text or "request" in name else "unexpected"


def _failure_state(conn, previous, attempted, exc):
    state = dict(previous or {})
    state.setdefault("last_event_time", None)
    state.setdefault("observed_completed_ids", [])
    state.setdefault(
        "ever_observed_completed_ids", state["observed_completed_ids"]
    )
    state.update(
        {
            "attempted_at": _iso(attempted),
            "succeeded_at": None,
            "error_class": _classify(exc),
            "freshness": False,
            "newly_observed_completed_workout_ids": [],
        }
    )
    _save(conn, state)


def _clean(value, fallback):
    text = " ".join(str(value or "").split())
    return text or fallback


def _number(value):
    value = float(value)
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.1f}".rstrip("0").rstrip(".")


def _duration(seconds):
    if seconds is None:
        return "duration unknown"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except (TypeError, ValueError):
        return "date unknown"


def _exercise_summary(rows):
    exercises = OrderedDict()
    for order, (template_id, name, set_type, weight, reps) in enumerate(rows):
        clean_name = _clean(name, "Exercise")
        key = str(template_id) if template_id is not None else clean_name
        exercise = exercises.setdefault(
            key, {"name": clean_name, "sets": 0, "weighted": [], "reps": []}
        )
        exercise["sets"] += 1
        if str(set_type or "normal").lower() == "warmup":
            continue
        if reps is not None and float(reps) > 0:
            exercise["reps"].append((float(reps), order))
        if (
            weight is not None
            and reps is not None
            and float(weight) > 0
            and float(reps) > 0
        ):
            weight = float(weight)
            reps = float(reps)
            exercise["weighted"].append(
                (weight * (1 + reps / 30), weight, reps, order)
            )

    lines = []
    total_volume = 0.0
    for exercise in exercises.values():
        weighted = exercise["weighted"]
        volume = sum(weight * reps for _, weight, reps, _ in weighted)
        total_volume += volume
        if weighted:
            _, weight, reps, _ = max(weighted, key=lambda item: item[:3])
            top = f"{_number(weight)} kg × {_number(reps)}"
            volume_text = f"{_number(volume)} kg"
        elif exercise["reps"]:
            reps, _ = max(exercise["reps"], key=lambda item: item[0])
            top = f"{_number(reps)} reps"
            volume_text = "n/a"
        else:
            top = "n/a"
            volume_text = "n/a"
        lines.append(
            f"• {exercise['name']}: top {top} · volume {volume_text}"
        )
    return lines, len(exercises), len(rows), total_volume


def _set_rows(conn, workout_id):
    return conn.execute(
        """
        SELECT exercise_template_id, exercise_name, set_type, weight_kg, reps
        FROM hevy_sets
        WHERE workout_id = ?
        ORDER BY set_index NULLS LAST, id
        """,
        [workout_id],
    ).fetchall()


def _workout_report(conn, workout_id):
    workout = conn.execute(
        """
        SELECT title, start_time, duration_seconds
        FROM hevy_workouts
        WHERE id = ? AND end_time IS NOT NULL
        """,
        [workout_id],
    ).fetchone()
    if not workout:
        raise ValueError("completed workout disappeared after commit")

    title, start_time, duration_seconds = workout
    set_rows = _set_rows(conn, workout_id)
    exercise_lines, exercise_count, set_count, volume = _exercise_summary(set_rows)
    date = _date(start_time)
    lines = [
        f"🏋️ {_clean(title, 'Workout')} — {date} · {_duration(duration_seconds)}",
        f"{exercise_count} exercise{'s' if exercise_count != 1 else ''} · "
        f"{set_count} set{'s' if set_count != 1 else ''}",
        *exercise_lines,
    ]

    previous = conn.execute(
        """
        SELECT id, start_time
        FROM hevy_workouts
        WHERE title IS NOT DISTINCT FROM ?
          AND id <> ?
          AND end_time IS NOT NULL
          AND start_time < ?
        ORDER BY start_time DESC, id DESC
        LIMIT 1
        """,
        [title, workout_id, start_time],
    ).fetchone()
    if previous:
        previous_id, previous_start = previous
        previous_rows = _set_rows(conn, previous_id)
        _, _, previous_set_count, previous_volume = _exercise_summary(previous_rows)
        comparison = []
        if previous_volume > 0:
            percent = (volume - previous_volume) / previous_volume * 100
            comparison.append(f"volume {percent:+.1f}%")
        set_delta = set_count - previous_set_count
        comparison.append("sets unchanged" if set_delta == 0 else f"sets {set_delta:+d}")
        lines.append(f"Vs previous {_date(previous_start)}: " + " · ".join(comparison))

    return "\n".join(lines)


def _workout_reports(conn, workout_ids):
    dated_ids = []
    for workout_id in workout_ids:
        row = conn.execute(
            "SELECT start_time FROM hevy_workouts WHERE id = ?", [workout_id]
        ).fetchone()
        if row:
            dated_ids.append((row[0], workout_id))
    dated_ids.sort(key=lambda item: (item[0], item[1]))
    return "\n\n".join(_workout_report(conn, workout_id) for _, workout_id in dated_ids)


def run(backfill=False):
    attempted = _now()
    conn = None
    previous = None
    try:
        with RunLock(LOCK_PATH):
            conn = duckdb.connect(str(sync_hevy.DB_PATH))
            previous = _state(conn)
            prior = sync_hevy.get_sync_state(conn, "last_event_time")
            if prior is not None:
                _parse(prior)
            old_ids = (
                set(
                    previous.get(
                        "ever_observed_completed_ids",
                        previous.get("observed_completed_ids", []),
                    )
                )
                if previous
                else set()
            )
            run_start = attempted
            deadline = time.monotonic() + sync_hevy._settings()[3]
            conn.execute("BEGIN")
            pilot_enabled = _pilot_enabled()
            pilot_profile = _pilot_profile() if pilot_enabled else None
            with contextlib.redirect_stdout(io.StringIO()):
                sync_hevy.sync_hevy(
                    backfill=backfill,
                    conn=conn,
                    run_start=run_start,
                    deadline=deadline,
                    include_metadata=backfill,
                )
            expected_sync = _iso(run_start)
            expected_cursor = _iso(sync_hevy.cursor_boundary(run_start))
            actual_sync = _parse(sync_hevy.get_sync_state(conn, "last_sync"))
            actual_cursor = _parse(sync_hevy.get_sync_state(conn, "last_event_time"))
            if (
                actual_sync != _parse(expected_sync)
                or actual_cursor != _parse(expected_cursor)
            ):
                raise FreshnessError("freshness validation failed")
            current = {
                str(row[0])
                for row in conn.execute(
                    "SELECT id FROM hevy_workouts WHERE end_time IS NOT NULL"
                ).fetchall()
            }
            ever = old_ids | current
            new = sorted(current - old_ids) if previous else []
            state = {
                "attempted_at": expected_sync,
                "succeeded_at": _iso(_now()),
                "error_class": None,
                "freshness": True,
                "last_event_time": expected_cursor,
                "newly_observed_completed_workout_ids": new,
                "ever_observed_completed_workout_ids": sorted(ever),
                "observed_completed_ids": sorted(current),
            }
            _save(conn, state)

            pilot_first_run = False
            pilot_card = None
            pilot_checkin = {"status": "none"}
            if pilot_enabled:
                strength_pilot.refresh_evidence(conn, run_start)
                next_routine = strength_pilot.next_routine_key(conn)
                routines = strength_pilot.load_routines(conn)
                strength_pilot.validate_profile(
                    pilot_profile, routines, next_routine
                )
                pilot_first_run = strength_pilot.baseline_cron_integration(
                    conn, run_start
                )
                cached = strength_pilot.cached_card(conn, next_routine)
                if pilot_first_run or new or cached is None:
                    pilot_card = strength_pilot.generate_card(
                        conn, next_routine, pilot_profile, run_start
                    )
                elif cached is not None:
                    pilot_card = cached
                pilot_checkin = strength_pilot.claim_checkin(conn, run_start)
            conn.commit()

            # Reports are deliberately queried only after the sync transaction commits.
            if pilot_enabled and pilot_first_run:
                print("NO_REPLY")
                return 0
            output = []
            if new:
                output.append(_workout_reports(conn, new))
                if pilot_enabled:
                    output.append(pilot_card["card_text"])
            if pilot_enabled and pilot_checkin.get("status") == "prompt":
                output.append(pilot_checkin["prompt"])
            print("\n\n".join(part for part in output if part) or "NO_REPLY")
            return 0
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.execute("BEGIN")
                _failure_state(conn, previous, attempted, exc)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
        print("HEVY_SYNC_FAILED")
        print("attempted_at=" + _iso(attempted))
        print("error_class=" + _classify(exc))
        print(f"error={type(exc).__name__}: {exc}")
        return 1
    finally:
        if conn is not None:
            conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reliable Hevy sync wrapper")
    parser.add_argument("--backfill", action="store_true")
    return run(parser.parse_args(argv).backfill)


if __name__ == "__main__":
    raise SystemExit(main())
