#!/usr/bin/env python3
"""
Import Health Auto Export Medications CSV files into DuckDB.

Usage:
    python src/import_medications.py <csv_file>
"""

import duckdb
import pandas as pd
import sys
from pathlib import Path
from datetime import datetime
from config import get_db_path

DB_PATH = get_db_path()


def import_medications_csv(csv_path, file_hash=None, is_reimport=False):
    """
    Import a Medications CSV into DuckDB.

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
        # Check if already imported
        existing = conn.execute(
            "SELECT import_id, file_hash FROM imports WHERE filename = ?", [filename]
        ).fetchone()
        if existing and not is_reimport:
            print(f"⏭️  Already imported: {filename} (import_id={existing[0]})")
            return 0

        print(f"📖 Reading: {filename}")
        df = pd.read_csv(csv_path)

        if "Date" not in df.columns:
            print(f"❌ Missing 'Date' column in {filename}")
            return -1

        # Skip archived
        if "Archived" in df.columns:
            df = df[df["Archived"] != "Yes"]

        # Parse dates (format: "2026-02-06 22:19:17 -0800")
        df["timestamp"] = pd.to_datetime(df["Date"], utc=True)
        df["scheduled_at"] = pd.to_datetime(df.get("Scheduled Date"), utc=True, errors="coerce")

        # Build insert df
        df_insert = pd.DataFrame({
            "timestamp": df["timestamp"],
            "scheduled_at": df["scheduled_at"],
            "medication": df["Medication"],
            "dosage": pd.to_numeric(df.get("Dosage"), errors="coerce"),
            "scheduled_dosage": pd.to_numeric(df.get("Scheduled Dosage"), errors="coerce"),
            "unit": df.get("Unit", ""),
            "status": df.get("Status", ""),
        })

        df_insert = df_insert.dropna(subset=["timestamp", "medication"])

        print(f"💊 Found {len(df_insert)} medication records")

        rows_before = conn.execute("SELECT COUNT(*) FROM medications").fetchone()[0]

        conn.execute("""
            INSERT OR IGNORE INTO medications (timestamp, scheduled_at, medication, dosage, scheduled_dosage, unit, status)
            SELECT timestamp, scheduled_at, medication, dosage, scheduled_dosage, unit, status
            FROM df_insert
        """)

        rows_after = conn.execute("SELECT COUNT(*) FROM medications").fetchone()[0]
        rows_added = rows_after - rows_before

        # Log import (insert or update depending on whether this is a re-import)
        if is_reimport and existing:
            conn.execute("""
                UPDATE imports 
                SET imported_at = ?, rows_added = ?, file_hash = ?
                WHERE filename = ?
            """, [datetime.now(), rows_added, file_hash, filename])
            import_id = existing[0]
            print(f"✅ Re-imported {rows_added} medication records (import_id={import_id}, updated hash)")
        else:
            conn.execute("""
                INSERT INTO imports (filename, imported_at, rows_added, source, file_hash)
                VALUES (?, ?, ?, 'medications', ?)
            """, [filename, datetime.now(), rows_added, file_hash])
            import_id = conn.execute(
                "SELECT import_id FROM imports WHERE filename = ?", [filename]
            ).fetchone()[0]
            print(f"✅ Imported {rows_added} medication records (import_id={import_id})")
        
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
        print("Usage: python src/import_medications.py <csv_file>")
        sys.exit(1)
    rows = import_medications_csv(sys.argv[1])
    sys.exit(0 if rows >= 0 else 1)
