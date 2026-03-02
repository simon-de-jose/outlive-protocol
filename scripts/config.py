"""
Configuration loader for outlive-protocol.

Reads config.yaml and provides path helpers for database, logs, and iCloud folder.
All other scripts should import from this module instead of hardcoding paths.
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


def get_db_path():
    """
    Get path to DuckDB database.
    
    Returns:
        Path: Absolute path to health.duckdb
    """
    config = load_config()
    db_path = config.get('data', {}).get('db_path')
    
    if not db_path:
        # Fallback to default (relative to project root)
        db_path = str(Path(__file__).parent.parent / 'data' / 'health.duckdb')
    
    path = Path(db_path).expanduser()
    
    # If relative, make it relative to project root
    if not path.is_absolute():
        path = (Path(__file__).parent.parent / path).resolve()
    
    return path


def get_log_dir():
    """
    Get path to log directory.
    
    Returns:
        Path: Absolute path to logs directory
    """
    config = load_config()
    log_dir = config.get('data', {}).get('log_dir')
    
    if not log_dir:
        # Fallback to default (relative to project root)
        log_dir = str(Path(__file__).parent.parent / 'data' / 'logs')
    
    path = Path(log_dir).expanduser()
    
    # If relative, make it relative to project root
    if not path.is_absolute():
        path = (Path(__file__).parent.parent / path).resolve()
    
    # Create directory if it doesn't exist
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
        # Fallback to default
        icloud_folder = str(
            Path.home() / 'Library' / 'Mobile Documents' / 
            'com~apple~CloudDocs' / 'Health Data'
        )
    
    return Path(icloud_folder).expanduser()


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


def get_reports_dir():
    """
    Get path to reports directory.
    
    Returns:
        Path: Absolute path to reports directory
    """
    config = load_config()
    reports_dir = config.get('data', {}).get('reports_dir')
    
    if not reports_dir:
        # Fallback to default (relative to project root)
        reports_dir = str(Path(__file__).parent.parent / 'data' / 'reports')
    
    path = Path(reports_dir).expanduser()
    
    # If relative, make it relative to project root
    if not path.is_absolute():
        path = (Path(__file__).parent.parent / path).resolve()
    
    # Create directory if it doesn't exist
    path.mkdir(parents=True, exist_ok=True)
    
    return path


def get_libre_csv_prefix():
    """
    Get the LibreView CSV filename prefix used to identify glucose files.
    
    Returns:
        str: Prefix string (e.g., "YourName_glucose_"). Empty string matches all glucose CSVs.
    """
    config = load_config()
    return config.get('libre_csv_prefix', '')
