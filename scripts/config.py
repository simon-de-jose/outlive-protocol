"""
Configuration loader for outlive-protocol.

Reads config.yaml and provides path helpers for database, logs, data files,
and user profile. All other scripts should import from this module instead
of hardcoding paths.

Path resolution priority:
1. Explicit path in config (e.g. data.db_path) — used as-is
2. Derived from data.data_dir (e.g. data_dir/health.duckdb)
3. Fallback to repo-relative defaults (e.g. ./data/health.duckdb)
"""

import yaml
from pathlib import Path

# Config file is in project root
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load_config():
    """Load and parse config.yaml"""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {CONFIG_PATH}. "
            "Copy config.example.yaml to config.yaml and customize it."
        )
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _resolve_path(path_str, default_relative=None):
    """Resolve a path string: expand ~, make absolute relative to project root."""
    if path_str:
        path = Path(path_str).expanduser()
    elif default_relative:
        path = Path(__file__).parent.parent / default_relative
    else:
        return None

    if not path.is_absolute():
        path = (Path(__file__).parent.parent / path).resolve()

    return path


def get_data_dir():
    """
    Get the consolidated data directory.

    All user data (DB, logs, reports, recipes, gurus, etc.) lives here.
    If data.data_dir is set, use it. Otherwise fall back to repo data/.

    Returns:
        Path: Absolute path to data directory
    """
    config = load_config()
    data_dir = config.get('data', {}).get('data_dir')

    path = _resolve_path(data_dir, default_relative='data')

    # Create if it doesn't exist
    path.mkdir(parents=True, exist_ok=True)

    return path


def get_db_path():
    """
    Get path to DuckDB database.

    Priority: data.db_path > data_dir/health.duckdb > repo/data/health.duckdb

    Returns:
        Path: Absolute path to health.duckdb
    """
    config = load_config()
    db_path = config.get('data', {}).get('db_path')

    if db_path:
        return _resolve_path(db_path)

    return get_data_dir() / 'health.duckdb'


def get_log_dir():
    """
    Get path to log directory.

    Priority: data.log_dir > data_dir/logs/ > repo/data/logs/

    Returns:
        Path: Absolute path to logs directory
    """
    config = load_config()
    log_dir = config.get('data', {}).get('log_dir')

    if log_dir:
        path = _resolve_path(log_dir)
    else:
        path = get_data_dir() / 'logs'

    path.mkdir(parents=True, exist_ok=True)
    return path


def get_reports_dir():
    """
    Get path to reports directory.

    Priority: data.reports_dir > data_dir/reports/ > repo/data/reports/

    Returns:
        Path: Absolute path to reports directory
    """
    config = load_config()
    reports_dir = config.get('data', {}).get('reports_dir')

    if reports_dir:
        path = _resolve_path(reports_dir)
    else:
        path = get_data_dir() / 'reports'

    path.mkdir(parents=True, exist_ok=True)
    return path


def get_icloud_folder():
    """
    Get path to iCloud Health Auto Export folder.

    Returns:
        Path: Absolute path to iCloud folder containing CSV exports
    """
    config = load_config()
    icloud_folder = config.get('data', {}).get('icloud_folder')

    if not icloud_folder:
        icloud_folder = str(
            Path.home() / 'Library' / 'Mobile Documents' /
            'com~apple~CloudDocs' / 'Health Data'
        )

    return Path(icloud_folder).expanduser()


def get_recipes_path():
    """
    Get path to recipes.json.

    Returns:
        Path: Absolute path to recipes.json in data_dir
    """
    return get_data_dir() / 'recipes.json'


def get_gurus_path():
    """
    Get path to gurus.json.

    Returns:
        Path: Absolute path to gurus.json in data_dir
    """
    return get_data_dir() / 'gurus.json'


def get_digest_state_path():
    """
    Get path to digest-state.json.

    Returns:
        Path: Absolute path to digest-state.json in data_dir
    """
    return get_data_dir() / 'digest-state.json'


def get_user_profile():
    """
    Load user profile from data_dir/user-profile.yaml.

    Returns:
        dict: User profile (libre_patient_name, nutrition_defaults, etc.)
              Returns empty dict if file doesn't exist.
    """
    profile_path = get_data_dir() / 'user-profile.yaml'

    if not profile_path.exists():
        return {}

    with open(profile_path) as f:
        return yaml.safe_load(f) or {}


def get_owner():
    """
    Get owner name from config.

    Returns:
        str: Owner name (e.g., "YourName")
    """
    config = load_config()
    return config.get('owner', 'Unknown')


def get_display_units():
    """
    Get display unit preference (metric or imperial).

    Returns:
        str: "metric" or "imperial"
    """
    config = load_config()
    return config.get('display', {}).get('units', 'metric')


def get_libre_csv_prefix():
    """
    Get the LibreView CSV filename prefix used to identify glucose files.

    Returns:
        str: Prefix string (e.g., "YourName_glucose_"). Empty string matches all.
    """
    config = load_config()
    return config.get('libre_csv_prefix', '')
