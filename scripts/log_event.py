#!/usr/bin/env python3
"""
Log a health event or life annotation to the database.

Events are life occurrences that may impact health metrics:
- Sleep disruptions (cats, neighbors, stress)
- Social activities (parties, gatherings)
- Travel (timezone changes, flights)
- Health events (illness, injuries)
- Stress events (work, personal)
- Diet changes (fasting, new foods)
- Exercise variations (rest days, competitions)
- Environmental factors (weather, air quality)

Usage:
    python src/log_event.py \\
        --timestamp "2026-02-11 01:30" \\
        --category sleep \\
        --tags cats,late_bedtime \\
        --impact immediate \\
        --note "Went to bed at 1am. Cappuccino bullied by Croissant at 3am."
    
    python src/log_event.py \\
        --timestamp "2026-02-10T18:00:00" \\
        --category social \\
        --impact short \\
        --note "Dinner party with friends, stayed out late"
"""

import argparse
import duckdb
import sys
from datetime import datetime
from pathlib import Path
from config import get_db_path

DB_PATH = get_db_path()

# Valid categories
VALID_CATEGORIES = [
    'sleep', 'social', 'travel', 'health', 'stress', 
    'diet', 'exercise', 'environment', 'misc'
]

# Valid impact windows
VALID_IMPACTS = ['immediate', 'short', 'long']


def parse_timestamp(ts_str: str) -> str:
    """
    Parse timestamp string to ISO format.
    
    Accepts:
    - ISO format: "2026-02-11T01:30:00"
    - Simple format: "2026-02-11 01:30"
    
    Returns:
        str: ISO formatted timestamp
    """
    # Try ISO format first
    for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"]:
        try:
            dt = datetime.strptime(ts_str, fmt)
            return dt.isoformat()
        except ValueError:
            continue
    
    raise ValueError(f"Invalid timestamp format: {ts_str}. Use 'YYYY-MM-DD HH:MM' or ISO format.")


def log_event(timestamp: str, category: str, note: str, 
              tags: list = None, impact: str = 'immediate', 
              logged_at: str = None) -> int:
    """
    Log an event to the database.
    
    Args:
        timestamp: When the event occurred (ISO format)
        category: Event category (sleep, social, travel, etc.)
        note: Description of the event
        tags: Optional list of tags
        impact: Impact window (immediate, short, long)
        logged_at: When the event was logged (defaults to now)
    
    Returns:
        int: Event ID
    """
    
    # Validate category
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category '{category}'. Must be one of: {', '.join(VALID_CATEGORIES)}")
    
    # Validate impact
    if impact not in VALID_IMPACTS:
        raise ValueError(f"Invalid impact '{impact}'. Must be one of: {', '.join(VALID_IMPACTS)}")
    
    # Default logged_at to now
    if logged_at is None:
        logged_at = datetime.now().isoformat()
    
    # Connect to database
    conn = duckdb.connect(str(DB_PATH))
    
    try:
        # Get next event_id
        result = conn.execute("SELECT nextval('seq_events')").fetchone()
        event_id = result[0]
        
        # Insert event
        conn.execute("""
            INSERT INTO events (id, timestamp, logged_at, category, tags, note, impact_window)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [event_id, timestamp, logged_at, category, tags, note, impact])
        
        return event_id
        
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Log a health event or life annotation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  # Sleep disruption
  python src/log_event.py --timestamp "2026-02-11 01:30" \\
      --category sleep --tags cats,late_bedtime \\
      --note "Went to bed at 1am. Cappuccino bullied by Croissant at 3am."
  
  # Social event
  python src/log_event.py --timestamp "2026-02-10 18:00" \\
      --category social --impact short \\
      --note "Dinner party with friends, stayed out late"

Valid categories: {', '.join(VALID_CATEGORIES)}
Valid impact windows: {', '.join(VALID_IMPACTS)}
        """
    )
    
    parser.add_argument(
        '--timestamp', 
        required=True,
        help='Event timestamp (ISO format or "YYYY-MM-DD HH:MM")'
    )
    
    parser.add_argument(
        '--category',
        required=True,
        choices=VALID_CATEGORIES,
        help='Event category'
    )
    
    parser.add_argument(
        '--note',
        required=True,
        help='Event description'
    )
    
    parser.add_argument(
        '--tags',
        help='Comma-separated tags (e.g., "cats,late_bedtime")'
    )
    
    parser.add_argument(
        '--impact',
        default='immediate',
        choices=VALID_IMPACTS,
        help='Impact window (default: immediate)'
    )
    
    parser.add_argument(
        '--logged-at',
        help='When the event was logged (ISO format, defaults to now)'
    )
    
    args = parser.parse_args()
    
    try:
        # Parse timestamp
        timestamp = parse_timestamp(args.timestamp)
        
        # Parse tags
        tags = None
        if args.tags:
            tags = [tag.strip() for tag in args.tags.split(',')]
        
        # Log event
        event_id = log_event(
            timestamp=timestamp,
            category=args.category,
            note=args.note,
            tags=tags,
            impact=args.impact,
            logged_at=args.logged_at
        )
        
        print(f"✅ Logged event #{event_id}")
        print(f"   Category: {args.category}")
        print(f"   Timestamp: {timestamp}")
        if tags:
            print(f"   Tags: {', '.join(tags)}")
        print(f"   Impact: {args.impact}")
        print(f"   Note: {args.note[:60]}{'...' if len(args.note) > 60 else ''}")
        
        sys.exit(0)
        
    except Exception as e:
        print(f"❌ Error logging event: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
