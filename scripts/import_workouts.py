#!/usr/bin/env python3
"""
Import Health Auto Export Workouts CSV files into DuckDB.

Usage:
    python src/import_workouts.py <csv_file>
"""

import duckdb
import pandas as pd
import sys
from pathlib import Path
from datetime import datetime
from config import get_db_path

DB_PATH = get_db_path()


def parse_duration(dur_str):
    """Parse 'HH:MM:SS' to seconds."""
    if not dur_str or pd.isna(dur_str):
        return None
    try:
        parts = str(dur_str).split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, TypeError):
        pass
    return None


def safe_float(val):
    """Convert to float or None."""
    try:
        if pd.isna(val) or val == "":
            return None
        return float(val)
    except (ValueError, TypeError):
        return None


def safe_int(val):
    """Convert to int or None."""
    f = safe_float(val)
    return int(f) if f is not None else None


def import_workouts_csv(csv_path, file_hash=None, is_reimport=False):
    """
    Import a Workouts CSV into DuckDB.

    Args:
        csv_path: Path to CSV file
        file_hash: SHA-256 hash of the file (for change detection)
        is_reimport: True if re-importing a changed file

    Returns:
        int: Number of rows imported, or -1 on error
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        print(f"❌ File not found: {csv_path}")
        return -1

    filename = csv_path.name
    conn = duckdb.connect(str(DB_PATH))

    try:
        existing = conn.execute(
            "SELECT import_id, file_hash FROM imports WHERE filename = ?", [filename]
        ).fetchone()
        if existing and not is_reimport:
            print(f"⏭️  Already imported: {filename} (import_id={existing[0]})")
            return 0

        print(f"📖 Reading: {filename}")
        df = pd.read_csv(csv_path)

        if "Type" not in df.columns or "Start" not in df.columns:
            print(f"❌ Missing required columns in {filename}")
            return -1

        records = []
        for _, row in df.iterrows():
            records.append({
                "start_time": pd.to_datetime(row.get("Start"), errors="coerce"),
                "end_time": pd.to_datetime(row.get("End"), errors="coerce"),
                "type": row["Type"],
                "duration_seconds": parse_duration(row.get("Duration")),
                "total_energy_kcal": safe_float(row.get("Total Energy (kcal)")),
                "active_energy_kcal": safe_float(row.get("Active Energy (kcal)")),
                "max_heart_rate": safe_float(row.get("Max Heart Rate (bpm)")),
                "avg_heart_rate": safe_float(row.get("Avg Heart Rate (bpm)")),
                "distance_km": safe_float(row.get("Distance (km)")),
                "step_count": safe_int(row.get("Step Count (count)")),
            })

        df_insert = pd.DataFrame(records)
        df_insert = df_insert.dropna(subset=["start_time", "type"])

        print(f"🏋️ Found {len(df_insert)} workout records")

        rows_before = conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0]

        conn.execute("""
            INSERT OR IGNORE INTO workouts 
                (start_time, end_time, type, duration_seconds, total_energy_kcal, 
                 active_energy_kcal, max_heart_rate, avg_heart_rate, distance_km, step_count)
            SELECT start_time, end_time, type, duration_seconds, total_energy_kcal,
                   active_energy_kcal, max_heart_rate, avg_heart_rate, distance_km, step_count
            FROM df_insert
        """)

        rows_after = conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0]
        rows_added = rows_after - rows_before

        # Log import (insert or update depending on whether this is a re-import)
        if is_reimport and existing:
            conn.execute("""
                UPDATE imports 
                SET imported_at = ?, rows_added = ?, file_hash = ?
                WHERE filename = ?
            """, [datetime.now(), rows_added, file_hash, filename])
            import_id = existing[0]
            print(f"✅ Re-imported {rows_added} workout records (import_id={import_id}, updated hash)")
        else:
            conn.execute("""
                INSERT INTO imports (filename, imported_at, rows_added, source, file_hash)
                VALUES (?, ?, ?, 'workouts', ?)
            """, [filename, datetime.now(), rows_added, file_hash])
            import_id = conn.execute(
                "SELECT import_id FROM imports WHERE filename = ?", [filename]
            ).fetchone()[0]
            print(f"✅ Imported {rows_added} workout records (import_id={import_id})")
        
        return rows_added

    except Exception as e:
        print(f"❌ Error importing {filename}: {e}")
        import traceback
        traceback.print_exc()
        return -1
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python src/import_workouts.py <csv_file>")
        sys.exit(1)
    rows = import_workouts_csv(sys.argv[1])
    sys.exit(0 if rows >= 0 else 1)
