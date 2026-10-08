#!/usr/bin/env python3
"""Explicit locked/versioned migration for the nutrition ingest schema."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from bootstrap.env import db_path
from nutrition_ingest import migrate_database


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate nutrition ingest schema")
    parser.add_argument("--db", help="Database path; defaults to HEALTH_DB_PATH")
    args = parser.parse_args()
    target = args.db or str(db_path())
    migrate_database(target)
    print(f"✅ Migrated nutrition ingest database: {target}")


if __name__ == "__main__":
    main()
