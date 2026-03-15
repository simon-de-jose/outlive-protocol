"""Shared env config for outlive-protocol. Reads .env from repo root."""

import os
from pathlib import Path
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

def _path(env_key, default=None):
    val = os.environ.get(env_key, default)
    return Path(val).expanduser().resolve() if val else None

def data_dir():
    d = _path("HEALTH_DATA_DIR", "~/health-data")
    d.mkdir(parents=True, exist_ok=True)
    return d

def db_path():
    return _path("HEALTH_DB_PATH") or data_dir() / "health.duckdb"

def log_dir():
    d = _path("HEALTH_LOG_DIR") or data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d

def reports_dir():
    d = _path("HEALTH_REPORTS_DIR") or data_dir() / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d

def icloud_folder():
    return _path("HEALTH_ICLOUD_FOLDER",
                 "~/Library/Mobile Documents/com~apple~CloudDocs/Health Data")

def libre_csv_prefix():
    return os.environ.get("LIBRE_CSV_PREFIX", "")

def owner():
    return os.environ.get("HEALTH_OWNER", "")

def units():
    return os.environ.get("HEALTH_UNITS", "metric")

def recipes_path():
    return data_dir() / "recipes.json"

def user_profile_path():
    """User profile YAML (optional, for libre sync patient matching)."""
    return data_dir() / "user-profile.yaml"
