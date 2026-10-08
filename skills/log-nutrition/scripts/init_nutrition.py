#!/usr/bin/env python3
"""Backward-compatible explicit nutrition schema initializer."""
from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path: sys.path.insert(0, str(REPO_ROOT))
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path: sys.path.insert(0, str(SCRIPT_DIR))
from bootstrap.env import db_path
from nutrition_ingest import migrate_database


def init_nutrition_table(path: str | Path | None = None) -> None:
    """Compatibility entry point used by bootstrap/init_db.py.

    Migration owns all writable connections; this wrapper intentionally opens
    no follow-on raw connection.
    """
    migrate_database(path or db_path())


if __name__ == "__main__":
    init_nutrition_table()
