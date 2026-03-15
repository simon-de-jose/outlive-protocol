#!/usr/bin/env bash
# Resolve all outlive-protocol paths from config.yaml
# Zero dependencies - just bash + grep/sed
# Usage: bash paths.sh [--json]

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$REPO_ROOT/config.yaml"

if [ ! -f "$CONFIG" ]; then
  echo "ERROR: config.yaml not found at $CONFIG" >&2
  echo "Copy config.example.yaml to config.yaml and customize it." >&2
  exit 1
fi

get_yaml() {
  grep "^  *$1:" "$CONFIG" | head -1 | sed "s/^[[:space:]]*$1:[[:space:]]*//" | sed 's/#.*//' | sed 's/[[:space:]]*$//'
}

get_yaml_top() {
  grep "^$1:" "$CONFIG" | head -1 | sed "s/^$1:[[:space:]]*//" | sed 's/#.*//' | sed 's/[[:space:]]*$//'
}

expand() { echo "${1/#\~/$HOME}"; }

VENV=$(expand "$(get_yaml_top venv)")
DATA_DIR=$(expand "$(get_yaml data_dir)")
ICLOUD=$(expand "$(get_yaml icloud_folder)")

# Individual paths: use explicit config if set, otherwise derive from data_dir
DB_EXPLICIT=$(get_yaml db_path)
LOG_EXPLICIT=$(get_yaml log_dir)
REPORTS_EXPLICIT=$(get_yaml reports_dir)

if [ -n "$DB_EXPLICIT" ]; then
  DB=$(expand "$DB_EXPLICIT")
else
  DB="$DATA_DIR/health.duckdb"
fi

if [ -n "$LOG_EXPLICIT" ]; then
  LOGS=$(expand "$LOG_EXPLICIT")
else
  LOGS="$DATA_DIR/logs"
fi

if [ -n "$REPORTS_EXPLICIT" ]; then
  REPORTS=$(expand "$REPORTS_EXPLICIT")
else
  REPORTS="$DATA_DIR/reports"
fi

if [ "$1" = "--json" ]; then
  printf "{\n"
  printf "  \"repo\": \"%s\",\n" "$REPO_ROOT"
  printf "  \"skills\": \"%s\",\n" "$REPO_ROOT/skills"
  printf "  \"data\": \"%s\",\n" "$REPO_ROOT/data"
  printf "  \"shell\": \"%s\",\n" "$REPO_ROOT/shell"
  printf "  \"config\": \"%s\",\n" "$CONFIG"
  printf "  \"venv\": \"%s\",\n" "$VENV"
  printf "  \"data_dir\": \"%s\",\n" "$DATA_DIR"
  printf "  \"db\": \"%s\",\n" "$DB"
  printf "  \"logs\": \"%s\",\n" "$LOGS"
  printf "  \"reports\": \"%s\",\n" "$REPORTS"
  printf "  \"icloud\": \"%s\"\n" "$ICLOUD"
  printf "}\n"
else
  echo "repo=$REPO_ROOT"
  echo "skills=$REPO_ROOT/skills"
  echo "data=$REPO_ROOT/data"
  echo "shell=$REPO_ROOT/shell"
  echo "config=$CONFIG"
  echo "venv=$VENV"
  echo "data_dir=$DATA_DIR"
  echo "db=$DB"
  echo "logs=$LOGS"
  echo "reports=$REPORTS"
  echo "icloud=$ICLOUD"
fi
