"""Tests for log-nutrition skill: recipes validation, script imports."""

import json
import sys
import subprocess
from pathlib import Path

import pytest

from bootstrap.env import recipes_path

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "log-nutrition" / "scripts"


def test_recipes_valid_json():
    rp = recipes_path()
    if not rp.exists():
        pytest.skip(f"recipes.json not found at {rp}")
    data = json.loads(rp.read_text())
    assert "recipes" in data
    assert isinstance(data["recipes"], list)
    if data["recipes"]:
        r = data["recipes"][0]
        for key in ["id", "name", "ingredients"]:
            assert key in r, f"Recipe missing '{key}'"


@pytest.mark.parametrize("module", ["log_nutrition", "init_nutrition"])
def test_script_import(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, '{SCRIPTS_DIR}'); import {module}"],
        capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    assert result.returncode == 0, f"Failed to import {module}: {result.stderr[:120]}"
