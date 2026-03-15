"""Config helper for sync-health-data. Reads config.yaml from repo root."""

import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # scripts/ → skill/ → skills/ → repo
CONFIG_PATH = REPO_ROOT / "config.yaml"

_config = None


def _load():
    global _config
    if _config is None:
        if not CONFIG_PATH.exists():
            raise FileNotFoundError(
                f"config.yaml not found at {CONFIG_PATH}. "
                "Copy config.example.yaml to config.yaml and customize it."
            )
        with open(CONFIG_PATH) as f:
            _config = yaml.safe_load(f)
    return _config


def _resolve(path_str):
    """Expand ~ and return absolute Path."""
    return Path(path_str).expanduser().resolve() if path_str else None


def get_data_dir():
    data_dir = _load().get("data", {}).get("data_dir")
    if data_dir:
        path = _resolve(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return REPO_ROOT / "data"


def get_db_path():
    db = _load().get("data", {}).get("db_path")
    if db:
        return _resolve(db)
    return get_data_dir() / "health.duckdb"


def get_icloud_folder():
    icloud = _load().get("data", {}).get("icloud_folder")
    if icloud:
        return Path(icloud).expanduser()
    return Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Health Data"


def get_libre_csv_prefix():
    return _load().get("libre_csv_prefix", "")


def get_log_dir():
    log_dir = _load().get("data", {}).get("log_dir")
    if log_dir:
        path = _resolve(log_dir)
    else:
        path = get_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_user_profile():
    """Load user profile from data_dir/user-profile.yaml."""
    profile_path = get_data_dir() / "user-profile.yaml"
    if not profile_path.exists():
        return {}
    with open(profile_path) as f:
        return yaml.safe_load(f) or {}
