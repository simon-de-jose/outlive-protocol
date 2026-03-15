#!/usr/bin/env python3
"""
Initialize Hevy-related tables in the health database.

Creates tables for:
- hevy_exercises: Exercise template catalog
- hevy_workouts: Workout sessions
- hevy_sets: Individual sets within workouts
- coach_routines: Local routine definitions (SoT)
- coach_progression: Progressive overload tracking
- hevy_sync_state: Sync metadata

Usage:
    python3 scripts/init_hevy.py
"""

import duckdb
from bootstrap.env import db_path

DB_PATH = db_path()


def init_hevy_tables():
    """Create Hevy and coaching tables if they don't exist."""

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(DB_PATH))

    try:
        # Exercise catalog (synced from Hevy)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hevy_exercises (
                template_id VARCHAR PRIMARY KEY,
                title VARCHAR NOT NULL,
                type VARCHAR,
                primary_muscle_group VARCHAR,
                secondary_muscle_groups VARCHAR,
                is_custom BOOLEAN DEFAULT FALSE,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Workout sessions (synced from Hevy)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hevy_workouts (
                id VARCHAR PRIMARY KEY,
                title VARCHAR,
                routine_id VARCHAR,
                description VARCHAR,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                duration_seconds INTEGER,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Individual sets within workouts
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hevy_sets (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_hevy_set_id'),
                workout_id VARCHAR NOT NULL,
                exercise_template_id VARCHAR NOT NULL,
                exercise_name VARCHAR,
                set_index INTEGER,
                set_type VARCHAR,
                weight_kg DOUBLE,
                reps INTEGER,
                distance_meters DOUBLE,
                duration_seconds DOUBLE,
                rpe DOUBLE,
                custom_metric DOUBLE
            )
        """)

        # Local routine definitions (source of truth)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coach_routines (
                id VARCHAR PRIMARY KEY,
                hevy_routine_id VARCHAR,
                title VARCHAR NOT NULL,
                split_type VARCHAR,
                day_label VARCHAR,
                exercises TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Progressive overload tracking
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coach_progression (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_coach_prog_id'),
                exercise_template_id VARCHAR NOT NULL,
                date DATE NOT NULL,
                estimated_1rm_kg DOUBLE,
                best_set_weight_kg DOUBLE,
                best_set_reps INTEGER,
                total_volume_kg DOUBLE,
                total_sets INTEGER,
                notes VARCHAR
            )
        """)

        # Sync state
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hevy_sync_state (
                key VARCHAR PRIMARY KEY,
                value VARCHAR,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        print("✅ Hevy tables created successfully")

        # Show table status
        for table in ['hevy_exercises', 'hevy_workouts', 'hevy_sets',
                      'coach_routines', 'coach_progression', 'hevy_sync_state']:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"   {table}: {count} rows")

    finally:
        conn.close()


def init_sequences():
    """Create sequences for auto-increment IDs."""
    conn = duckdb.connect(str(DB_PATH))
    try:
        for seq in ['seq_hevy_set_id', 'seq_coach_prog_id']:
            try:
                conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq} START 1")
            except Exception:
                pass  # Already exists
    finally:
        conn.close()


def main():
    print("🏋️ Initializing Hevy tables...")
    init_sequences()
    init_hevy_tables()


if __name__ == "__main__":
    main()
