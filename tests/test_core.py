"""Cross-cutting tests: git hygiene, skill files, example files, personal data."""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


def run_cmd(cmd, cwd=None):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd or str(REPO_ROOT))
    return r.returncode, r.stdout.strip(), r.stderr.strip()


# ── Skill Files ──────────────────────────────────────────

SKILL_CHECKS = [
    ("skills/sync-health-data/SKILL.md", ["HealthKit", "LibreView"]),
    ("skills/analyze-health-data/SKILL.md", ["Attia", "readings"]),
    ("skills/log-nutrition/SKILL.md", ["USDA", "nutrition_log", "recipe"]),
    ("skills/coach-strength/SKILL.md", ["Hevy", "progressive", "hevy_workouts"]),
    ("skills/coach-cardio/SKILL.md", ["Zone 2", "VO2 max", "workouts"]),
    ("skills/coach-nutrition/SKILL.md", ["protein", "glucose", "nutrition_log"]),
]


@pytest.mark.parametrize("rel_path,keywords", SKILL_CHECKS)
def test_skill_md_exists_and_has_keywords(rel_path, keywords):
    path = REPO_ROOT / rel_path
    assert path.exists(), f"{rel_path} not found"
    content = path.read_text()
    assert content.startswith("---"), f"{rel_path} missing frontmatter"
    for kw in keywords:
        assert kw in content, f"{rel_path} missing keyword '{kw}'"


def test_no_sub_skill_refs():
    for skill_dir in (REPO_ROOT / "skills").iterdir():
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            assert "sub-skill" not in skill_md.read_text(), f"{skill_dir.name}/SKILL.md has 'sub-skill'"


def test_references_mentioned_in_skill_md():
    for skill_dir in (REPO_ROOT / "skills").iterdir():
        refs_dir = skill_dir / "references"
        if not refs_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        content = skill_md.read_text() if skill_md.exists() else ""
        for ref_file in refs_dir.glob("*.md"):
            assert ref_file.name in content, f"{skill_dir.name} doesn't reference {ref_file.name}"


def test_no_personal_data_in_skill_files():
    personal = ["Juan", "Lilliana", "haishan", "~/clawd/"]
    for skill_dir in (REPO_ROOT / "skills").iterdir():
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            content = skill_md.read_text()
            for p in personal:
                assert p not in content, f"{skill_dir.name}/SKILL.md contains '{p}'"


# ── No Personal Data in Tracked Files ────────────────────

def test_no_personal_data_in_tracked_files():
    rc, tracked_files, _ = run_cmd("git ls-files")
    assert rc == 0
    personal_patterns = ["Juan", "Lilliana", "Liliana", "moltbot", "haishan", "~/clawd/", "croissant"]
    violations = []
    for filepath in tracked_files.split("\n"):
        if not filepath or filepath.startswith(".git"):
            continue
        # Skip test files themselves
        if "test_" in filepath:
            continue
        if not any(filepath.endswith(ext) for ext in [".md", ".py", ".yaml", ".yml", ".json", ".sh"]):
            continue
        full_path = REPO_ROOT / filepath
        if not full_path.exists():
            continue
        try:
            content = full_path.read_text()
            for pattern in personal_patterns:
                if pattern in content:
                    violations.append(f"{filepath}: '{pattern}'")
        except UnicodeDecodeError:
            continue
    assert len(violations) == 0, f"Personal data found: {'; '.join(violations[:5])}"


def test_personal_files_not_tracked():
    personal_files = ["config.yaml", ".env", "data/recipes.json", "data/gurus.json", "data/digest-state.json"]
    for pf in personal_files:
        rc, _, _ = run_cmd(f"git ls-files --error-unmatch {pf} 2>/dev/null")
        assert rc != 0, f"'{pf}' should NOT be tracked in git"


# ── Example Files ────────────────────────────────────────

def test_env_example_exists():
    env_example = REPO_ROOT / ".env.example"
    assert env_example.exists()
    content = env_example.read_text()
    assert "USDA_API_KEY" in content
    assert "HEVY_API_KEY" in content
    assert "HEALTH_DATA_DIR" in content
    assert "rQVp" not in content, ".env.example contains real API key"


def test_example_data_files_exist():
    for name in ["recipes.example.json", "gurus.example.json",
                 "digest-state.example.json", "user-profile.example.yaml"]:
        path = REPO_ROOT / "data" / name
        assert path.exists(), f"Missing example file: {name}"


def test_pyproject_toml_exists():
    pyproject = REPO_ROOT / "pyproject.toml"
    assert pyproject.exists()
    content = pyproject.read_text()
    for dep in ["duckdb", "pandas", "python-dotenv"]:
        assert dep in content, f"pyproject.toml missing dep '{dep}'"


def test_init_db_exists():
    assert (REPO_ROOT / "bootstrap" / "init_db.py").exists()


def test_gitignore_has_patterns():
    gitignore = REPO_ROOT / ".gitignore"
    assert gitignore.exists()
    content = gitignore.read_text()
    for pattern in [".env", "data/recipes.json", "data/gurus.json", "data/digest-state.json"]:
        assert pattern in content, f".gitignore missing '{pattern}'"


# ── Git Hygiene ──────────────────────────────────────────

def test_no_duckdb_tracked():
    rc, stdout, _ = run_cmd("git ls-files '*.duckdb' '*.duckdb.wal'")
    assert stdout == "", "DuckDB files should not be tracked"


# ── Data Files (needs user data dir) ────────────────────

def test_data_files():
    from bootstrap.env import data_dir, recipes_path, user_profile_path

    # Recipes
    rp = recipes_path()
    if rp.exists():
        data = json.loads(rp.read_text())
        assert "recipes" in data
        assert isinstance(data["recipes"], list)
        if data["recipes"]:
            r = data["recipes"][0]
            for key in ["id", "name", "ingredients"]:
                assert key in r, f"Recipe missing '{key}'"

    # Gurus
    gurus_path = data_dir() / "gurus.json"
    if gurus_path.exists():
        data = json.loads(gurus_path.read_text())
        assert "gurus" in data

    # User profile
    pp = user_profile_path()
    if pp.exists():
        import yaml
        with open(pp) as f:
            profile = yaml.safe_load(f) or {}
        assert isinstance(profile, dict)
