"""Tests for bootstrap/env.py config resolution."""

import os
import yaml
from pathlib import Path

from bootstrap.env import db_path, data_dir, log_dir, icloud_folder, libre_csv_prefix, recipes_path, user_profile_path

REPO_ROOT = Path(__file__).parent.parent


def test_db_path_resolves():
    db = db_path()
    assert db is not None
    assert str(db).endswith(".duckdb")
    assert db.is_absolute()


def test_data_dir_resolves():
    d = data_dir()
    assert d is not None
    assert d.exists()
    assert d.is_absolute()


def test_icloud_folder_resolves():
    icloud = icloud_folder()
    assert icloud is not None
    assert icloud.is_absolute()


def test_libre_csv_prefix_is_string():
    prefix = libre_csv_prefix()
    assert isinstance(prefix, str)


def test_recipes_path_resolves():
    r = recipes_path()
    assert r is not None
    assert r.is_absolute()


def test_user_profile_path_resolves():
    p = user_profile_path()
    assert p is not None
    assert p.is_absolute()


def test_explicit_db_path_override(monkeypatch):
    """If HEALTH_DB_PATH is set, it takes priority over data_dir default."""
    monkeypatch.setenv("HEALTH_DB_PATH", "/tmp/test-override.duckdb")
    from importlib import reload
    import bootstrap.env
    reload(bootstrap.env)
    result = bootstrap.env.db_path()
    assert result.name == "test-override.duckdb"
    assert "tmp" in str(result)
    # Restore
    monkeypatch.delenv("HEALTH_DB_PATH")
    reload(bootstrap.env)


def test_explicit_log_dir_override(monkeypatch):
    """If HEALTH_LOG_DIR is set, it takes priority."""
    monkeypatch.setenv("HEALTH_LOG_DIR", "/tmp/test-logs")
    from importlib import reload
    import bootstrap.env
    reload(bootstrap.env)
    result = bootstrap.env.log_dir()
    assert result.name == "test-logs"
    assert "tmp" in str(result)
    monkeypatch.delenv("HEALTH_LOG_DIR")
    reload(bootstrap.env)


def test_init_db_loads():
    """bootstrap.init_db can be imported."""
    import bootstrap.init_db
    assert hasattr(bootstrap.init_db, "init_database")
