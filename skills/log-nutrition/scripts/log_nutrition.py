#!/usr/bin/env python3
"""Nutrition CLI backed solely by the locked shared ingest API."""
from __future__ import annotations
import argparse
import json
import sys
import warnings
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path: sys.path.insert(0, str(REPO_ROOT))
from bootstrap.env import db_path
from nutrition_ingest import identity_from_payload, ingest_nutrition


def log_nutrition_result(data: dict, *, allow_anonymous: bool = False) -> dict:
    """Return the committed/replayed durable visible result, never retry input."""
    identity = identity_from_payload(data)
    if identity is None and not allow_anonymous:
        raise ValueError("nutrition writes require provider and message_id; pass --allow-anonymous only for non-idempotent imports")
    if identity is None:
        warnings.warn("anonymous nutrition write is non-idempotent", RuntimeWarning, stacklevel=2)
    return ingest_nutrition(db_path(), data, identity=identity)["result"]


def log_nutrition(data: dict, *, allow_anonymous: bool = False) -> int:
    """Compatibility wrapper retained for existing callers."""
    return int(log_nutrition_result(data, allow_anonymous=allow_anonymous)["entry"]["entry_id"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Log nutrition entry")
    parser.add_argument("--json", required=True, help="JSON data for the entry")
    parser.add_argument(
        "--allow-anonymous",
        action="store_true",
        help="allow a non-idempotent import without provider/message_id (warning emitted)",
    )
    args = parser.parse_args()
    # Stable serialization makes a fresh-process retry byte-identical.
    print(json.dumps(log_nutrition_result(json.loads(args.json), allow_anonymous=args.allow_anonymous), ensure_ascii=False, sort_keys=True, separators=(",", ":")))

if __name__ == "__main__": main()
