#!/usr/bin/env python3
"""
Sync workout data from Hevy API into the local health database.

Features:
- Incremental sync via /v1/workouts/events (updates + deletes)
- Full backfill on first run via /v1/workouts
- Exercise template catalog sync
- Routine sync (Hevy → local coach_routines)
- Progressive overload calculation after sync

Auth: HEVY_API_KEY from .env file in repo root.

Usage:
    python3 scripts/sync_hevy.py              # Incremental sync
    python3 scripts/sync_hevy.py --backfill   # Full backfill
    python3 scripts/sync_hevy.py --dry-run    # Preview without writing
    python3 scripts/sync_hevy.py --exercises  # Sync exercise templates only
    python3 scripts/sync_hevy.py --routines   # Sync routines only
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import duckdb
import requests

from bootstrap.env import db_path

# API config
API_BASE = "https://api.hevyapp.com/v1"
DB_PATH = db_path()
RETRY_AFTER_CAP = 30.0
MAX_REPLAY_OVERLAP_SECONDS = 3600.0


class HevySyncError(RuntimeError):
    """Base exception for failures specific to Hevy synchronization."""


class HevyConfigurationError(HevySyncError):
    """Raised when required or numeric sync configuration is invalid."""


class HevyTimeoutError(TimeoutError, HevySyncError):
    """Raised when request retries or the overall run deadline expire."""


class HevyConnectionError(ConnectionError, HevySyncError):
    """Raised when connection retries are exhausted."""


def _finite_number(name, default, minimum=0.0, maximum=None):
    """Read and validate a finite numeric environment setting."""
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise HevyConfigurationError(f"invalid {name}: {raw!r}") from exc

    if (not math.isfinite(value) or value < minimum
            or (maximum is not None and value > maximum)):
        raise HevyConfigurationError(f"invalid {name}: {raw!r}")
    return value


def _settings():
    """Return validated request, retry, deadline, and replay settings."""
    timeout = _finite_number("HEVY_SYNC_TIMEOUT_SECONDS", 30, 0.001)
    raw_retries = os.environ.get("HEVY_SYNC_MAX_RETRIES", "3")
    try:
        retries = int(raw_retries)
    except (TypeError, ValueError) as exc:
        raise HevyConfigurationError(
            f"invalid HEVY_SYNC_MAX_RETRIES: {raw_retries!r}"
        ) from exc
    if str(retries) != str(raw_retries).strip() or retries < 0:
        raise HevyConfigurationError(
            f"invalid HEVY_SYNC_MAX_RETRIES: {raw_retries!r}"
        )

    backoff = _finite_number("HEVY_SYNC_BACKOFF_SECONDS", 1)
    deadline = _finite_number("HEVY_SYNC_DEADLINE_SECONDS", 300, 0.001)
    overlap = _finite_number(
        "HEVY_SYNC_REPLAY_OVERLAP_SECONDS",
        300,
        0,
        MAX_REPLAY_OVERLAP_SECONDS,
    )
    return timeout, retries, backoff, deadline, overlap


def replay_overlap():
    """Return the validated event replay overlap in seconds."""
    return _settings()[4]


def cursor_boundary(run_start):
    """Return the durable cursor boundary for a run's fixed start time."""
    return run_start.astimezone(timezone.utc) - timedelta(seconds=replay_overlap())


def _retry_after(value):
    """Parse Retry-After seconds/date and cap server-directed sleeping."""
    if not value:
        return None
    try:
        delay = float(value)
        if not math.isfinite(delay):
            return None
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value).astimezone(timezone.utc)
            delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(RETRY_AFTER_CAP, max(0.0, delay))


def get_api_key():
    """Load Hevy API key from env (loaded by bootstrap.env from .env)."""
    key = os.environ.get("HEVY_API_KEY")
    if key and key != "your_hevy_api_key_here":
        return key

    raise HevyConfigurationError(
        "HEVY_API_KEY not found. Add it to .env or set it as an environment "
        "variable (get a key at https://hevy.com/settings?developer)."
    )


def api_get(endpoint, params=None, *, deadline=None):
    """Make authenticated GET request to Hevy API."""
    timeout, retries, backoff, configured_deadline, _ = _settings()
    if deadline is None:
        deadline = time.monotonic() + configured_deadline

    headers = {
        "accept": "application/json",
        "api-key": get_api_key()
    }
    url = f"{API_BASE}{endpoint}"
    transient_statuses = {408, 429} | set(range(500, 600))

    for attempt in range(retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HevyTimeoutError("sync deadline exhausted")

        try:
            resp = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=min(timeout, remaining),
            )
            if resp.status_code in transient_statuses and attempt < retries:
                delay = _retry_after(resp.headers.get("Retry-After"))
                if delay is None:
                    delay = min(RETRY_AFTER_CAP, backoff * (2 ** attempt))
                if time.monotonic() + delay >= deadline:
                    raise HevyTimeoutError("sync deadline exhausted")
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.Timeout as exc:
            if attempt >= retries:
                raise HevyTimeoutError(
                    "request timeout retry exhaustion"
                ) from exc
            delay = min(RETRY_AFTER_CAP, backoff * (2 ** attempt))
            if time.monotonic() + delay >= deadline:
                raise HevyTimeoutError("sync deadline exhausted") from exc
            time.sleep(delay)
        except requests.ConnectionError as exc:
            if attempt >= retries:
                raise HevyConnectionError(
                    "connection retry exhaustion"
                ) from exc
            delay = min(RETRY_AFTER_CAP, backoff * (2 ** attempt))
            if time.monotonic() + delay >= deadline:
                raise HevyTimeoutError("sync deadline exhausted") from exc
            time.sleep(delay)

    raise HevyTimeoutError("retry exhaustion")


def get_sync_state(conn, key):
    """Get a sync state value."""
    result = conn.execute(
        "SELECT value FROM hevy_sync_state WHERE key = ?", [key]
    ).fetchone()
    return result[0] if result else None


def set_sync_state(conn, key, value):
    """Set a sync state value."""
    now = datetime.now().isoformat()
    conn.execute("""
        INSERT INTO hevy_sync_state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (key) DO UPDATE SET value = ?, updated_at = ?
    """, [key, value, now, value, now])


def sync_exercises(conn, dry_run=False, *, deadline=None):
    """Sync exercise templates from Hevy."""
    print("\n📋 Syncing exercise templates...")

    page = 1
    total = 0

    while True:
        data = api_get(
            "/exercise_templates",
            {"page": page, "pageSize": 100},
            deadline=deadline,
        )
        templates = data.get("exercise_templates", [])

        if not templates:
            break

        for t in templates:
            if not dry_run:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute("""
                    INSERT INTO hevy_exercises (template_id, title, type,
                        primary_muscle_group, secondary_muscle_groups, is_custom, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (template_id) DO UPDATE SET
                        title = ?, type = ?, primary_muscle_group = ?,
                        secondary_muscle_groups = ?, is_custom = ?, synced_at = ?
                """, [
                    t["id"], t["title"], t.get("type"),
                    t.get("primary_muscle_group"),
                    json.dumps(t.get("secondary_muscle_groups", [])),
                    t.get("is_custom", False), now,
                    t["title"], t.get("type"), t.get("primary_muscle_group"),
                    json.dumps(t.get("secondary_muscle_groups", [])),
                    t.get("is_custom", False), now
                ])
            total += 1

        if page >= data.get("page_count", 1):
            break
        page += 1
        time.sleep(0.2)  # Rate limiting

    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"   {prefix}{total} exercise templates synced")
    return total


def upsert_workout(conn, workout):
    """Insert or update a workout and its sets."""

    # Calculate duration
    duration = None
    if workout.get("start_time") and workout.get("end_time"):
        try:
            start = datetime.fromisoformat(workout["start_time"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(workout["end_time"].replace("Z", "+00:00"))
            duration = int((end - start).total_seconds())
        except (ValueError, TypeError):
            pass

    # Upsert workout
    now = datetime.now().isoformat()
    conn.execute("""
        INSERT INTO hevy_workouts (id, title, routine_id, description,
            start_time, end_time, duration_seconds, created_at, updated_at, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            title = ?, routine_id = ?, description = ?,
            start_time = ?, end_time = ?, duration_seconds = ?,
            updated_at = ?, synced_at = ?
    """, [
        workout["id"], workout.get("title"), workout.get("routine_id"),
        workout.get("description"),
        workout.get("start_time"), workout.get("end_time"),
        duration, workout.get("created_at"), workout.get("updated_at"), now,
        workout.get("title"), workout.get("routine_id"),
        workout.get("description"),
        workout.get("start_time"), workout.get("end_time"),
        duration, workout.get("updated_at"), now
    ])

    # Delete existing sets for this workout (for clean re-import)
    conn.execute("DELETE FROM hevy_sets WHERE workout_id = ?", [workout["id"]])

    # Insert sets
    for exercise in workout.get("exercises", []):
        for s in exercise.get("sets", []):
            conn.execute("""
                INSERT INTO hevy_sets (workout_id, exercise_template_id, exercise_name,
                    set_index, set_type, weight_kg, reps, distance_meters,
                    duration_seconds, rpe, custom_metric)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                workout["id"],
                exercise.get("exercise_template_id"),
                exercise.get("title"),
                s.get("index"),
                s.get("type"),
                s.get("weight_kg"),
                s.get("reps"),
                s.get("distance_meters"),
                s.get("duration_seconds"),
                s.get("rpe"),
                s.get("custom_metric")
            ])


def _advance_sync_state(conn, run_start, backfill=False):
    """Advance freshness state only after a successful workout sync."""
    run_start_utc = run_start.astimezone(timezone.utc).isoformat()
    if backfill:
        set_sync_state(conn, "last_backfill", run_start_utc)
    set_sync_state(conn, "last_sync", run_start_utc)
    set_sync_state(conn, "last_event_time", cursor_boundary(run_start).isoformat())


def sync_workouts_backfill(conn, dry_run=False, *, run_start=None, deadline=None):
    """Full backfill of all workouts."""
    print("\n🏋️ Backfilling all workouts...")
    run_start = run_start or datetime.now(timezone.utc)

    count_data = api_get("/workouts/count", deadline=deadline)
    total_count = count_data.get("workout_count", 0)
    print(f"   Total workouts in Hevy: {total_count}")

    if total_count == 0:
        print("   No workouts to sync")
        if not dry_run:
            _advance_sync_state(conn, run_start, backfill=True)
        return 0

    page = 1
    synced = 0

    while True:
        data = api_get(
            "/workouts",
            {"page": page, "pageSize": 10},
            deadline=deadline,
        )
        workouts = data.get("workouts", [])

        if not workouts:
            break

        for w in workouts:
            if not dry_run:
                upsert_workout(conn, w)
            synced += 1
            sets_count = sum(len(e.get("sets", [])) for e in w.get("exercises", []))
            print(f"   {'[DRY-RUN] ' if dry_run else ''}Synced: {w.get('title', 'Untitled')} "
                  f"({w['start_time'][:10]}) — {len(w.get('exercises', []))} exercises, {sets_count} sets")

        if page >= data.get("page_count", 1):
            break
        page += 1
        time.sleep(0.5)

    if not dry_run:
        _advance_sync_state(conn, run_start, backfill=True)

    print(f"\n   {'[DRY-RUN] ' if dry_run else ''}Total: {synced} workouts synced")
    return synced


def sync_workouts_incremental(conn, dry_run=False, *, run_start=None, deadline=None):
    """Incremental sync using /v1/workouts/events."""
    run_start = run_start or datetime.now(timezone.utc)
    last_event = get_sync_state(conn, "last_event_time")

    if not last_event:
        print("   No previous sync found — running full backfill")
        return sync_workouts_backfill(
            conn,
            dry_run,
            run_start=run_start,
            deadline=deadline,
        )

    print(f"\n🔄 Incremental sync (since {last_event})...")

    page = 1
    updated = 0
    deleted = 0
    while True:
        data = api_get("/workouts/events", {
            "page": page,
            "pageSize": 10,
            "since": last_event
        }, deadline=deadline)

        events = data.get("events", [])
        if not events:
            # Check for updated_workouts / deleted_workout_ids format
            updated_workouts = data.get("updated_workouts", [])
            deleted_ids = data.get("deleted_workout_ids", [])

            for w in updated_workouts:
                if not dry_run:
                    upsert_workout(conn, w)
                updated += 1
                print(f"   {'[DRY-RUN] ' if dry_run else ''}Updated: {w.get('title')} ({w['start_time'][:10]})")

            for wid in deleted_ids:
                if not dry_run:
                    conn.execute("DELETE FROM hevy_sets WHERE workout_id = ?", [wid])
                    conn.execute("DELETE FROM hevy_workouts WHERE id = ?", [wid])
                deleted += 1
                print(f"   {'[DRY-RUN] ' if dry_run else ''}Deleted: {wid}")

            if not updated_workouts and not deleted_ids:
                break
        else:
            # Handle events format
            for event in events:
                event_type = event.get("type")
                workout = event.get("workout")

                if event_type == "updated" and workout:
                    if not dry_run:
                        upsert_workout(conn, workout)
                    updated += 1
                elif event_type == "deleted":
                    wid = event.get("workout_id")
                    if wid and not dry_run:
                        conn.execute("DELETE FROM hevy_sets WHERE workout_id = ?", [wid])
                        conn.execute("DELETE FROM hevy_workouts WHERE id = ?", [wid])
                    deleted += 1

        if page >= data.get("page_count", 1):
            break
        page += 1
        time.sleep(0.2)

    if not dry_run:
        _advance_sync_state(conn, run_start)

    if updated == 0 and deleted == 0:
        print("   ✨ No changes since last sync")
    else:
        prefix = "[DRY-RUN] " if dry_run else ""
        print(f"\n   {prefix}{updated} updated, {deleted} deleted")

    return updated + deleted


def sync_routines(conn, dry_run=False, *, deadline=None):
    """Sync routines from Hevy → local coach_routines."""
    print("\n📝 Syncing routines...")

    page = 1
    total = 0

    while True:
        data = api_get(
            "/routines",
            {"page": page, "pageSize": 10},
            deadline=deadline,
        )
        routines = data.get("routines", [])

        if not routines:
            break

        for r in routines:
            exercises_json = json.dumps(r.get("exercises", []))

            if not dry_run:
                # Check if we already have this routine locally
                existing = conn.execute(
                    "SELECT id FROM coach_routines WHERE hevy_routine_id = ?",
                    [r["id"]]
                ).fetchone()

                if existing:
                    conn.execute("""
                        UPDATE coach_routines SET
                            title = ?, exercises = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE hevy_routine_id = ?
                    """, [r["title"], exercises_json, r["id"]])
                else:
                    import uuid
                    conn.execute("""
                        INSERT INTO coach_routines (id, hevy_routine_id, title, exercises,
                            created_at, updated_at)
                        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """, [str(uuid.uuid4()), r["id"], r["title"], exercises_json])

            total += 1
            exercise_count = len(r.get("exercises", []))
            print(f"   {'[DRY-RUN] ' if dry_run else ''}{r['title']} — {exercise_count} exercises")

        if page >= data.get("page_count", 1):
            break
        page += 1

    print(f"   {'[DRY-RUN] ' if dry_run else ''}{total} routines synced")
    return total


def update_progression(conn):
    """Calculate progressive overload metrics after sync."""
    print("\n📈 Updating progression tracking...")

    # Get all workouts with sets, grouped by exercise and date
    rows = conn.execute("""
        SELECT
            s.exercise_template_id,
            DATE(w.start_time) as workout_date,
            s.weight_kg,
            s.reps,
            s.set_type
        FROM hevy_sets s
        JOIN hevy_workouts w ON s.workout_id = w.id
        WHERE s.set_type = 'normal'
          AND s.weight_kg IS NOT NULL
          AND s.reps IS NOT NULL
          AND s.weight_kg > 0
        ORDER BY s.exercise_template_id, workout_date, s.set_index
    """).fetchall()

    # Group by exercise + date. Remove derived rows that disappeared after
    # workout edits/deletes before refreshing the remaining metrics.
    from collections import defaultdict
    groups = defaultdict(list)
    for template_id, date, weight, reps, set_type in rows:
        groups[(template_id, date)].append((weight, reps))

    existing_keys = set(conn.execute("""
        SELECT exercise_template_id, date FROM coach_progression
    """).fetchall())
    for template_id, date in existing_keys - set(groups):
        conn.execute("""
            DELETE FROM coach_progression
            WHERE exercise_template_id = ? AND date = ?
        """, [template_id, date])

    if not rows:
        print("   No workout data for progression tracking")
        return

    inserted = 0
    for (template_id, date), sets in groups.items():
        # Calculate metrics
        best_weight = max(w for w, r in sets)
        best_set = max(sets, key=lambda x: x[0] * (1 + x[1] / 30))  # Best e1RM
        estimated_1rm = best_set[0] * (1 + best_set[1] / 30)  # Epley
        total_volume = sum(w * r for w, r in sets)
        total_sets = len(sets)

        # Upsert
        conn.execute("""
            DELETE FROM coach_progression
            WHERE exercise_template_id = ? AND date = ?
        """, [template_id, date])

        conn.execute("""
            INSERT INTO coach_progression (exercise_template_id, date,
                estimated_1rm_kg, best_set_weight_kg, best_set_reps,
                total_volume_kg, total_sets)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [template_id, date, round(estimated_1rm, 2),
              best_set[0], best_set[1], round(total_volume, 2), total_sets])
        inserted += 1

    print(f"   {inserted} exercise/date progression entries updated")


def sync_hevy(backfill=False, dry_run=False, exercises_only=False,
              routines_only=False, conn=None, run_start=None, deadline=None,
              include_metadata=True):
    """Sync Hevy data, optionally skipping catalog/routine metadata.

    Manual callers retain the historical full-sync behavior.  The daily cron
    gate passes ``include_metadata=False`` so workout events are not blocked by
    rate limits while paging metadata that changes infrequently.
    """

    owns_connection = conn is None
    if conn is None:
        conn = duckdb.connect(str(DB_PATH))
    run_start = run_start or datetime.now(timezone.utc)
    if deadline is None:
        deadline = time.monotonic() + _settings()[3]

    try:
        if exercises_only:
            sync_exercises(conn, dry_run, deadline=deadline)
            return

        if routines_only:
            sync_routines(conn, dry_run, deadline=deadline)
            return

        if include_metadata:
            sync_exercises(conn, dry_run, deadline=deadline)

        if backfill:
            synced = sync_workouts_backfill(
                conn,
                dry_run,
                run_start=run_start,
                deadline=deadline,
            )
        else:
            synced = sync_workouts_incremental(
                conn,
                dry_run,
                run_start=run_start,
                deadline=deadline,
            )

        if include_metadata:
            sync_routines(conn, dry_run, deadline=deadline)

        if not dry_run and synced > 0:
            update_progression(conn)

        # Summary
        if not dry_run:
            workout_count = conn.execute("SELECT COUNT(*) FROM hevy_workouts").fetchone()[0]
            set_count = conn.execute("SELECT COUNT(*) FROM hevy_sets").fetchone()[0]
            exercise_count = conn.execute("SELECT COUNT(*) FROM hevy_exercises").fetchone()[0]
            routine_count = conn.execute("SELECT COUNT(*) FROM coach_routines").fetchone()[0]

            print(f"\n📊 DB Status:")
            print(f"   Exercises: {exercise_count}")
            print(f"   Workouts: {workout_count}")
            print(f"   Sets: {set_count}")
            print(f"   Routines: {routine_count}")

    finally:
        if owns_connection:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description="Sync Hevy workout data")
    parser.add_argument("--backfill", action="store_true", help="Full backfill of all workouts")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--exercises", action="store_true", help="Sync exercise templates only")
    parser.add_argument("--routines", action="store_true", help="Sync routines only")
    args = parser.parse_args()

    print("🏋️ Hevy Sync")
    print(f"   DB: {DB_PATH}")
    print(f"   Mode: {'backfill' if args.backfill else 'incremental'}"
          f"{'  [DRY-RUN]' if args.dry_run else ''}")

    try:
        sync_hevy(
            backfill=args.backfill,
            dry_run=args.dry_run,
            exercises_only=args.exercises,
            routines_only=args.routines
        )
    except Exception as exc:
        print(f"Hevy sync failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
