"""Config helper for coach-nutrition. Reads config.yaml from repo root."""

import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
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


def get_data_dir():
    data_dir = _load().get("data", {}).get("data_dir")
    if data_dir:
        path = Path(data_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return REPO_ROOT / "data"


def get_db_path():
    db = _load().get("data", {}).get("db_path")
    if db:
        return Path(db).expanduser().resolve()
    return get_data_dir() / "health.duckdb"
