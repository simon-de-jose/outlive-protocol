#!/usr/bin/env python3
"""
Query health events and life annotations from the database.

Supports filtering by date, category, tags, and text search.

Usage:
    # List recent events
    python src/query_events.py --list
    python src/query_events.py --list 20
    
    # Filter by date
    python src/query_events.py --date 2026-02-11
    
    # Filter by category
    python src/query_events.py --category sleep
    
    # Filter by tag
    python src/query_events.py --tag cats
    
    # Search notes
    python src/query_events.py --search "Cappuccino"
    
    # Combine filters
    python src/query_events.py --category sleep --tag cats --list 5
"""

import argparse
import duckdb
import sys
from datetime import datetime
from pathlib import Path
from config import get_db_path

DB_PATH = get_db_path()


def query_events(limit: int = 10, date: str = None, category: str = None, 
                 tag: str = None, search: str = None):
    """
    Query events from the database.
    
    Args:
        limit: Maximum number of events to return
        date: Filter by specific date (YYYY-MM-DD)
        category: Filter by category
        tag: Filter by tag
        search: Search in note text
    
    Returns:
        list: List of event rows
    """
    
    conn = duckdb.connect(str(DB_PATH))
    
    try:
        # Build query
        query = "SELECT id, timestamp, category, tags, note, impact_window, logged_at FROM events WHERE 1=1"
        params = []
        
        # Add date filter
        if date:
            query += " AND DATE(timestamp) = ?"
            params.append(date)
        
        # Add category filter
        if category:
            query += " AND category = ?"
            params.append(category)
        
        # Add tag filter (check if tag exists in array)
        if tag:
            query += " AND list_contains(tags, ?)"
            params.append(tag)
        
        # Add text search
        if search:
            query += " AND LOWER(note) LIKE ?"
            params.append(f"%{search.lower()}%")
        
        # Order by timestamp descending
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        
        # Execute query
        result = conn.execute(query, params)
        rows = result.fetchall()
        
        return rows
        
    finally:
        conn.close()


def format_event(row):
    """Format an event row for display."""
    event_id, timestamp, category, tags, note, impact, logged_at = row
    
    # Format timestamp (handle both string and datetime objects)
    if isinstance(timestamp, str):
        ts = datetime.fromisoformat(timestamp)
    else:
        ts = timestamp
    ts_str = ts.strftime("%Y-%m-%d %H:%M")
    
    # Format tags
    tags_str = ""
    if tags:
        tags_str = f" [{', '.join(tags)}]"
    
    # Format output
    lines = [
        f"Event #{event_id} | {ts_str} | {category}{tags_str}",
        f"  Impact: {impact}",
        f"  {note}",
    ]
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Query health events and life annotations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List 10 most recent events
  python src/query_events.py --list
  
  # List 20 most recent events
  python src/query_events.py --list 20
  
  # Events on a specific date
  python src/query_events.py --date 2026-02-11
  
  # Sleep events
  python src/query_events.py --category sleep
  
  # Events tagged with 'cats'
  python src/query_events.py --tag cats
  
  # Search for text in notes
  python src/query_events.py --search "Cappuccino"
  
  # Combine filters
  python src/query_events.py --category sleep --tag cats --list 5
        """
    )
    
    parser.add_argument(
        '--list',
        type=int,
        nargs='?',
        const=10,
        metavar='N',
        help='List N most recent events (default: 10)'
    )
    
    parser.add_argument(
        '--date',
        help='Filter by date (YYYY-MM-DD)'
    )
    
    parser.add_argument(
        '--category',
        help='Filter by category'
    )
    
    parser.add_argument(
        '--tag',
        help='Filter by tag'
    )
    
    parser.add_argument(
        '--search',
        help='Search in note text'
    )
    
    args = parser.parse_args()
    
    # Determine limit
    limit = 10
    if args.list is not None:
        limit = args.list
    
    try:
        # Query events
        events = query_events(
            limit=limit,
            date=args.date,
            category=args.category,
            tag=args.tag,
            search=args.search
        )
        
        if not events:
            print("No events found matching criteria.")
            sys.exit(0)
        
        # Display results
        print(f"Found {len(events)} event(s):\n")
        
        for i, event in enumerate(events):
            print(format_event(event))
            if i < len(events) - 1:
                print()  # Blank line between events
        
        sys.exit(0)
        
    except Exception as e:
        print(f"❌ Error querying events: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
