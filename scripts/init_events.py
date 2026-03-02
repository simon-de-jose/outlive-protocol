#!/usr/bin/env python3
"""
Initialize the events table for health annotations and life events.

Creates the events table for logging life events that may impact health metrics:
- Sleep disruptions
- Social activities
- Travel
- Stress events
- Diet changes
- Exercise variations
- Environmental factors

Usage:
    python src/init_events.py
"""

import duckdb
import sys
from pathlib import Path
from config import get_db_path

# Database location
DB_PATH = get_db_path()


def init_events_table():
    """Create events table if it doesn't exist."""
    
    # Ensure data directory exists
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    # Connect to database
    conn = duckdb.connect(str(DB_PATH))
    
    try:
        # Create sequence for auto-increment id
        conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS seq_events START 1
        """)
        
        # Create events table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_events'),
                timestamp TIMESTAMP NOT NULL,
                logged_at TIMESTAMP NOT NULL,
                category VARCHAR NOT NULL,
                tags VARCHAR[],
                note VARCHAR NOT NULL,
                impact_window VARCHAR DEFAULT 'immediate'
            )
        """)
        
        # Create indexes for common query patterns
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_timestamp 
            ON events(timestamp)
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_category 
            ON events(category)
        """)
        
        # Verify table exists
        tables = conn.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'main' AND table_name = 'events'
        """).fetchall()
        
        if tables:
            print(f"✅ Events table initialized: {DB_PATH}")
            print(f"✅ Ready to log health annotations and life events")
            return True
        else:
            print(f"❌ Failed to create events table")
            return False
        
    except Exception as e:
        print(f"❌ Error initializing events table: {e}")
        return False
    
    finally:
        conn.close()


if __name__ == "__main__":
    success = init_events_table()
    sys.exit(0 if success else 1)
