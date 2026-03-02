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
DB=$(expand "$(get_yaml db_path)")
LOGS=$(expand "$(get_yaml log_dir)")
REPORTS=$(expand "$(get_yaml reports_dir)")
ICLOUD=$(expand "$(get_yaml icloud_folder)")

if [ "$1" = "--json" ]; then
  printf "{\n"
  printf "  \"repo\": \"%s\",\n" "$REPO_ROOT"
  printf "  \"scripts\": \"%s\",\n" "$REPO_ROOT/scripts"
  printf "  \"data\": \"%s\",\n" "$REPO_ROOT/data"
  printf "  \"shell\": \"%s\",\n" "$REPO_ROOT/shell"
  printf "  \"config\": \"%s\",\n" "$CONFIG"
  printf "  \"venv\": \"%s\",\n" "$VENV"
  printf "  \"db\": \"%s\",\n" "$DB"
  printf "  \"logs\": \"%s\",\n" "$LOGS"
  printf "  \"reports\": \"%s\",\n" "$REPORTS"
  printf "  \"icloud\": \"%s\"\n" "$ICLOUD"
  printf "}\n"
else
  echo "repo=$REPO_ROOT"
  echo "scripts=$REPO_ROOT/scripts"
  echo "data=$REPO_ROOT/data"
  echo "shell=$REPO_ROOT/shell"
  echo "config=$CONFIG"
  echo "venv=$VENV"
  echo "db=$DB"
  echo "logs=$LOGS"
  echo "reports=$REPORTS"
  echo "icloud=$ICLOUD"
fi
