#!/bin/bash
# Trigger health data import after file upload.
# Calls daily_import.py directly — paths resolved via .env + bootstrap.env.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

cd "$REPO_ROOT"
PYTHON_BIN="${OUTLIVE_PYTHON:-$REPO_ROOT/.venv/bin/python3}"
PYTHONPATH="$REPO_ROOT" "$PYTHON_BIN" "$REPO_ROOT/skills/sync-health-data/scripts/daily_import.py" 2>&1

echo "[$(date -Iseconds)] Health import triggered via upload"
