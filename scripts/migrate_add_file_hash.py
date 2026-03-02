#!/usr/bin/env python3
"""
Migration: Add file_hash column to imports table.

This enables hash-based change detection for re-importing updated files.
"""

import duckdb
import sys
from pathlib import Path
from config import get_db_path

DB_PATH = get_db_path()


def migrate():
    """Add file_hash column to imports table if it doesn't exist."""
    conn = duckdb.connect(str(DB_PATH))
    
    try:
        # Check if column already exists
        columns = conn.execute("PRAGMA table_info(imports)").fetchall()
        column_names = [col[1] for col in columns]
        
        if 'file_hash' in column_names:
            print("✅ Column 'file_hash' already exists in imports table")
            return True
        
        # Add the column
        print("🔨 Adding file_hash column to imports table...")
        conn.execute("""
            ALTER TABLE imports 
            ADD COLUMN file_hash VARCHAR
        """)
        
        print("✅ Migration complete: file_hash column added")
        print("📝 Note: Existing imports will have NULL hash (backwards compatible)")
        
        return True
        
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    finally:
        conn.close()


if __name__ == "__main__":
    success = migrate()
    sys.exit(0 if success else 1)
