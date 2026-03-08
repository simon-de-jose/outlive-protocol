#!/bin/bash
# Trigger health data import after file upload.
# Uses paths.sh --json to resolve all paths — no hardcoding.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Resolve paths from config (JSON avoids space-in-path issues)
PATHS_JSON=$(bash "$REPO_ROOT/shell/paths.sh" --json)
VENV=$(echo "$PATHS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['venv'])")
SCRIPTS=$(echo "$PATHS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['scripts'])")

cd "$REPO_ROOT"
"$VENV" "$SCRIPTS/daily_import.py" 2>&1

echo "[$(date -Iseconds)] Health import triggered via upload"
