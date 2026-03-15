#!/bin/bash
# Trigger health data import after file upload.
# Calls daily_import.py directly — paths resolved via .env + bootstrap.env.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

cd "$REPO_ROOT"
python3 "$REPO_ROOT/skills/sync-health-data/scripts/daily_import.py" 2>&1

echo "[$(date -Iseconds)] Health import triggered via upload"
