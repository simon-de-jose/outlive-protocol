#!/usr/bin/env python3
"""
Import Health Auto Export CycleTracking CSV files into DuckDB.

CycleTracking CSVs have a different schema than HealthMetrics:
- Columns: Start, End, Data, Value, Cycle Start
- Uses "Start" as the timestamp
- Stores in readings table with metric names like "Cycle Tracking - Menstrual Flow"

Usage:
    python src/import_cycletracking.py <csv_file>
"""

import duckdb
import pandas as pd
import sys
from pathlib import Path
from datetime import datetime
from config import get_db_path

DB_PATH = get_db_path()


def import_cycletracking_csv(csv_path, file_hash=None, is_reimport=False):
    """
    Import a CycleTracking CSV into DuckDB readings table.
    
    Args:
        csv_path: Path to CycleTracking CSV file
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

        # Verify required columns exist
        required_cols = ['Start', 'Data', 'Value']
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            print(f"❌ Missing required columns: {', '.join(missing_cols)}")
            return -1

        # Convert Start to datetime
        df['Start'] = pd.to_datetime(df['Start'], errors='coerce')
        
        # Drop rows with invalid timestamps or missing data
        df = df.dropna(subset=['Start', 'Data', 'Value'])

        print(f"🩸 Found {len(df)} cycle tracking records")

        # Transform to readings format
        # Metric name format: "Cycle Tracking - {Data}"
        # e.g., "Cycle Tracking - Menstrual Flow"
        records = []
        for _, row in df.iterrows():
            metric_name = f"Cycle Tracking - {row['Data']}"
            
            # Store the value as-is (might be text like "Unspecified", "Light", etc.)
            # For numeric compatibility, we'll encode text values
            value_str = str(row['Value'])
            
            # Try to convert to numeric, otherwise encode common values
            try:
                numeric_value = float(value_str)
            except ValueError:
                # Encode text values to numbers for storage
                # This allows querying while preserving the meaning
                value_mapping = {
                    'Unspecified': 0.0,
                    'Light': 1.0,
                    'Medium': 2.0,
                    'Heavy': 3.0,
                    'None': 0.0,
                    'Yes': 1.0,
                    'No': 0.0,
                }
                numeric_value = value_mapping.get(value_str, 0.0)
            
            records.append({
                'timestamp': row['Start'],
                'metric': metric_name,
                'value': numeric_value,
                'unit': value_str,  # Store original value in unit field for reference
                'source': 'cycletracking'
            })

        df_insert = pd.DataFrame(records)
        
        # Insert into readings table
        rows_before = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

        conn.execute("""
            INSERT OR IGNORE INTO readings (timestamp, metric, value, unit, source)
            SELECT timestamp, metric, value, unit, source
            FROM df_insert
        """)

        rows_after = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        rows_added = rows_after - rows_before

        # Log import (insert or update depending on whether this is a re-import)
        if is_reimport and existing:
            conn.execute("""
                UPDATE imports 
                SET imported_at = ?, rows_added = ?, file_hash = ?
                WHERE filename = ?
            """, [datetime.now(), rows_added, file_hash, filename])
            import_id = existing[0]
            print(f"✅ Re-imported {rows_added} cycle tracking readings (import_id={import_id}, updated hash)")
        else:
            conn.execute("""
                INSERT INTO imports (filename, imported_at, rows_added, source, file_hash)
                VALUES (?, ?, ?, 'cycletracking', ?)
            """, [filename, datetime.now(), rows_added, file_hash])
            import_id = conn.execute(
                "SELECT import_id FROM imports WHERE filename = ?", [filename]
            ).fetchone()[0]
            print(f"✅ Imported {rows_added} cycle tracking readings (import_id={import_id})")
        
        # Show sample of what was imported
        if rows_added > 0:
            sample = conn.execute("""
                SELECT metric, COUNT(*) as count
                FROM readings
                WHERE source = 'cycletracking'
                GROUP BY metric
                ORDER BY count DESC
                LIMIT 5
            """).fetchall()
            
            if sample:
                print(f"📊 Metrics imported:")
                for metric, count in sample:
                    print(f"   - {metric}: {count} readings")

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
        print("Usage: python src/import_cycletracking.py <csv_file>")
        print("\nExample:")
        print('  python src/import_cycletracking.py "/path/to/CycleTracking-2026-01-20.csv"')
        sys.exit(1)
    
    rows = import_cycletracking_csv(sys.argv[1])
    sys.exit(0 if rows >= 0 else 1)
