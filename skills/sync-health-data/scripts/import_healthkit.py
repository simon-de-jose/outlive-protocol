#!/usr/bin/env python3
"""
Import Health Auto Export CSV files into DuckDB.

Transforms wide CSV format (124 columns) to long/normalized format.
Handles sparse data, deduplicates, and logs imports for idempotency.

Usage:
    python src/import_healthkit.py <csv_file>
    python src/import_healthkit.py "path/to/HealthMetrics-2026-02-05.csv"
"""

import duckdb
import pandas as pd
import sys
import re
from pathlib import Path
from datetime import datetime
from config import get_db_path

# Database location
DB_PATH = get_db_path()

def parse_metric_column(column_name):
    """
    Extract metric name and unit from column header.
    
    Examples:
        "Active Energy (kcal)" → ("Active Energy", "kcal")
        "Sleep Analysis [Total] (hr)" → ("Sleep Analysis [Total]", "hr")
        "Body Mass Index (count)" → ("Body Mass Index", "count")
    
    Returns:
        tuple: (metric_name, unit) or (column_name, "") if no unit found
    """
    # Pattern: anything followed by (unit)
    match = re.match(r'^(.+?)\s*\(([^)]+)\)$', column_name)
    if match:
        metric = match.group(1).strip()
        unit = match.group(2).strip()
        return metric, unit
    return column_name, ""

def import_csv(csv_path, file_hash=None, is_reimport=False, source="healthkit"):
    """
    Import a Health Auto Export CSV into DuckDB.
    
    Args:
        csv_path: Path to CSV file
        file_hash: SHA-256 hash of the file (for change detection)
        is_reimport: True if re-importing a changed file
        source: Data source identifier (default: "healthkit")
    
    Returns:
        int: Number of rows imported, or -1 on error
    """
    csv_path = Path(csv_path)
    
    if not csv_path.exists():
        print(f"❌ File not found: {csv_path}")
        return -1
    
    filename = csv_path.name
    
    # Connect to database
    conn = duckdb.connect(str(DB_PATH))
    
    try:
        # Check if already imported
        existing = conn.execute(
            "SELECT import_id, file_hash FROM imports WHERE filename = ?",
            [filename]
        ).fetchone()
        
        if existing and not is_reimport:
            print(f"⏭️  Already imported: {filename} (import_id={existing[0]})")
            return 0
        
        # Read CSV
        print(f"📖 Reading: {csv_path.name}")
        df = pd.read_csv(csv_path)
        
        # Verify Date/Time column exists
        if 'Date/Time' not in df.columns:
            print(f"❌ Missing 'Date/Time' column in {filename}")
            return -1
        
        # Convert Date/Time to datetime
        df['Date/Time'] = pd.to_datetime(df['Date/Time'])
        
        # Extract metric columns (everything except Date/Time)
        metric_columns = [col for col in df.columns if col != 'Date/Time']
        
        print(f"📊 Found {len(df)} rows, {len(metric_columns)} metric columns")
        
        # Melt wide → long format
        df_long = df.melt(
            id_vars=['Date/Time'],
            value_vars=metric_columns,
            var_name='metric_raw',
            value_name='value'
        )
        
        # Drop NaN values (sparse data)
        df_long = df_long.dropna(subset=['value'])
        
        print(f"🔄 Transformed to {len(df_long)} non-empty readings")
        
        # Parse metric names and units
        df_long[['metric', 'unit']] = df_long['metric_raw'].apply(
            lambda x: pd.Series(parse_metric_column(x))
        )
        
        # Prepare final dataframe
        df_final = df_long[['Date/Time', 'metric', 'value', 'unit']].copy()
        df_final.rename(columns={'Date/Time': 'timestamp'}, inplace=True)
        df_final['source'] = source
        
        # Remove duplicates (keep first occurrence)
        initial_count = len(df_final)
        df_final = df_final.drop_duplicates(subset=['timestamp', 'metric', 'source'], keep='first')
        dupe_count = initial_count - len(df_final)
        
        if dupe_count > 0:
            print(f"⚠️  Removed {dupe_count} duplicate readings")
        
        # Insert into database (ignore conflicts for idempotency)
        rows_before = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        
        # Use INSERT OR IGNORE to handle conflicts gracefully
        conn.execute("""
            INSERT OR IGNORE INTO readings (timestamp, metric, value, unit, source)
            SELECT timestamp, metric, value, unit, source
            FROM df_final
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
            print(f"✅ Re-imported {rows_added} new readings (import_id={import_id}, updated hash)")
        else:
            conn.execute("""
                INSERT INTO imports (filename, imported_at, rows_added, source, file_hash)
                VALUES (?, ?, ?, ?, ?)
            """, [filename, datetime.now(), rows_added, source, file_hash])
            import_id = conn.execute(
                "SELECT import_id FROM imports WHERE filename = ?",
                [filename]
            ).fetchone()[0]
            print(f"✅ Imported {rows_added} new readings (import_id={import_id})")
        
        # Show sample metrics imported
        sample_metrics = conn.execute("""
            SELECT metric, COUNT(*) as count
            FROM readings
            WHERE source = ?
            GROUP BY metric
            ORDER BY count DESC
            LIMIT 5
        """, [source]).fetchall()
        
        if sample_metrics:
            print(f"📈 Top metrics:")
            for metric, count in sample_metrics:
                print(f"   - {metric}: {count} readings")
        
        return rows_added
        
    except Exception as e:
        print(f"❌ Error importing {filename}: {e}")
        import traceback
        traceback.print_exc()
        return -1
    
    finally:
        conn.close()

def main():
    if len(sys.argv) < 2:
        print("Usage: python src/import_healthkit.py <csv_file>")
        print("\nExample:")
        print('  python src/import_healthkit.py "/path/to/HealthMetrics-2026-02-05.csv"')
        sys.exit(1)
    
    csv_path = sys.argv[1]
    rows = import_csv(csv_path)
    
    sys.exit(0 if rows >= 0 else 1)

if __name__ == "__main__":
    main()
