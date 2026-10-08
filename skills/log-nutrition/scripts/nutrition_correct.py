#!/usr/bin/env python3
"""Thin CLI adapter for the central deterministic nutrition correction API."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bootstrap.env import db_path
from nutrition_ingest import correct_nutrition_result


def nutrition_correct(payload: dict[str, Any]) -> dict[str, Any]:
    return correct_nutrition_result(db_path(), payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Correct an existing nutrition entry")
    parser.add_argument("--json", required=True, help="JSON data for the correction")
    args = parser.parse_args()
    try:
        payload = json.loads(args.json)
        result = nutrition_correct(payload)
    except Exception as exc:  # pragma: no cover - CLI guardrail
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
