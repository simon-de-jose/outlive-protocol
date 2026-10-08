#!/usr/bin/env python3
"""Thin CLI adapter for the central deterministic quick nutrition ingester."""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bootstrap.env import data_dir, db_path, user_profile_path
from nutrition_ingest import resolve_and_ingest_quick_text


def load_user_defaults() -> dict[str, Any]:
    """Load optional defaults without making the quick path depend on PyYAML."""
    path = user_profile_path()
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore
        return (yaml.safe_load(path.read_text()) or {}).get("nutrition_defaults", {}) or {}
    except Exception:
        return {}


def quick_log_text(payload: dict[str, Any], *, allow_anonymous: bool = False) -> dict[str, Any]:
    """Resolve and ingest through the public path-based central API only."""
    resolved_data_dir = data_dir()
    return resolve_and_ingest_quick_text(
        db_path(), payload, defaults_loaded=bool(load_user_defaults()), data_dir=str(resolved_data_dir),
        allow_anonymous=allow_anonymous,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast-path text nutrition logger")
    parser.add_argument("--json", required=True, help="Structured meal JSON payload")
    parser.add_argument(
        "--allow-anonymous",
        action="store_true",
        help="allow a non-idempotent import without provider/message_id (warning emitted)",
    )
    args = parser.parse_args()
    try:
        payload = json.loads(args.json)
        if args.allow_anonymous and not any(key in payload for key in ("discord_message_id", "provider", "message_id", "ingest_identity")):
            warnings.warn("anonymous quick nutrition write is non-idempotent", RuntimeWarning, stacklevel=1)
        result = quick_log_text(payload, allow_anonymous=args.allow_anonymous)
    except Exception as exc:  # pragma: no cover - CLI guardrail
        result = {"status": "error", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"logged", "already_logged", "needs_clarification"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
