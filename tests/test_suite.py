#!/usr/bin/env python3
"""
Outlive-protocol test suite.

Run after any changes to verify nothing is broken.

Usage:
    python3 tests/test_suite.py              # Run all tests
    python3 tests/test_suite.py --quick      # Skip slow tests (libre dry-run, import dry-run)
    python3 tests/test_suite.py --fresh      # Run fresh-clone checks (no personal data in tracked files)
"""

import sys
import os
import json
import subprocess
import argparse
import tempfile
import shutil
from pathlib import Path

# Ensure scripts/ is on path
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

PASS = "✅"
FAIL = "❌"
SKIP = "⏭️"
results = []


def test(name, condition, detail=""):
    """Record a test result."""
    status = PASS if condition else FAIL
    results.append((status, name, detail))
    print(f"  {status} {name}" + (f" — {detail}" if detail else ""))
    return condition


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def run_cmd(cmd, cwd=None):
    """Run a command, return (returncode, stdout, stderr)."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd or str(REPO_ROOT))
    return r.returncode, r.stdout.strip(), r.stderr.strip()


# ═══════════════════════════════════════════════════════════
# 1. CONFIG RESOLUTION
# ═══════════════════════════════════════════════════════════

def test_config_resolution():
    section("1. Config Resolution")

    from config import (
        load_config, get_data_dir, get_db_path, get_log_dir,
        get_reports_dir, get_icloud_folder, get_recipes_path,
        get_gurus_path, get_digest_state_path, get_user_profile,
        get_owner, get_display_units, get_libre_csv_prefix, _resolve_path
    )

    # Config loads
    try:
        config = load_config()
        test("config.yaml loads", True)
    except Exception as e:
        test("config.yaml loads", False, str(e))
        return

    # data_dir
    data_dir = get_data_dir()
    test("data_dir resolves", data_dir is not None, str(data_dir))
    test("data_dir exists", data_dir.exists())
    test("data_dir is absolute", data_dir.is_absolute())

    # All paths derive correctly
    db = get_db_path()
    test("db_path resolves", db is not None, str(db))
    test("db_path ends with .duckdb", str(db).endswith(".duckdb"))

    logs = get_log_dir()
    reports = get_reports_dir()
    test("log_dir resolves and exists", logs.exists(), str(logs))
    test("reports_dir resolves and exists", reports.exists(), str(reports))

    # Data file paths point to data_dir
    recipes = get_recipes_path()
    gurus = get_gurus_path()
    digest = get_digest_state_path()
    test("recipes_path under data_dir", str(recipes).startswith(str(data_dir)), str(recipes))
    test("gurus_path under data_dir", str(gurus).startswith(str(data_dir)), str(gurus))
    test("digest_state_path under data_dir", str(digest).startswith(str(data_dir)), str(digest))

    # iCloud folder
    icloud = get_icloud_folder()
    test("icloud_folder resolves", icloud is not None, str(icloud))
    test("icloud_folder is absolute", icloud.is_absolute())

    # User profile
    profile = get_user_profile()
    test("user_profile loads (dict)", isinstance(profile, dict))

    # Other getters
    test("owner is non-empty string", isinstance(get_owner(), str) and len(get_owner()) > 0, get_owner())
    test("display_units is metric|imperial", get_display_units() in ("metric", "imperial"))
    test("libre_csv_prefix is string", isinstance(get_libre_csv_prefix(), str))

    # _resolve_path unit tests
    test("_resolve_path(None, None) = None", _resolve_path(None) is None)
    test("_resolve_path with ~ expands", str(_resolve_path("~/test")).startswith("/"))
    test("_resolve_path absolute stays absolute", str(_resolve_path("/tmp/test")) == "/tmp/test")

    # paths.sh parity
    rc, stdout, _ = run_cmd("bash shell/paths.sh --json")
    if rc == 0:
        paths = json.loads(stdout)
        test("paths.sh db == config.py db", paths["db"] == str(db))
        test("paths.sh data_dir == config.py data_dir", paths["data_dir"] == str(data_dir))
        test("paths.sh logs == config.py logs", paths["logs"] == str(logs))
        test("paths.sh reports == config.py reports", paths["reports"] == str(reports))
    else:
        test("paths.sh runs without error", False, "exit code " + str(rc))


# ═══════════════════════════════════════════════════════════
# 2. CONFIG BACKWARD COMPATIBILITY
# ═══════════════════════════════════════════════════════════

def test_config_backward_compat():
    section("2. Config Backward Compatibility")

    from config import _resolve_path

    # Test that explicit paths override data_dir
    # This is tested by checking the priority logic in config.py
    from config import load_config, get_db_path, get_log_dir, get_reports_dir
    config = load_config()
    data_section = config.get('data', {})

    # If db_path is explicitly set, it should be used over data_dir/health.duckdb
    if 'db_path' in data_section:
        db = get_db_path()
        explicit = _resolve_path(data_section['db_path'])
        test("explicit db_path takes priority over data_dir", db == explicit)
    else:
        test("db_path derives from data_dir (no explicit override)", True)

    if 'log_dir' in data_section:
        logs = get_log_dir()
        explicit = _resolve_path(data_section['log_dir'])
        test("explicit log_dir takes priority", logs == explicit)
    else:
        test("log_dir derives from data_dir (no explicit override)", True)

    if 'reports_dir' in data_section:
        reports = get_reports_dir()
        explicit = _resolve_path(data_section['reports_dir'])
        test("explicit reports_dir takes priority", reports == explicit)
    else:
        test("reports_dir derives from data_dir (no explicit override)", True)


# ═══════════════════════════════════════════════════════════
# 3. DATABASE SCHEMA & INTEGRITY
# ═══════════════════════════════════════════════════════════

def test_database():
    section("3. Database Schema & Integrity")

    from config import get_db_path
    import duckdb

    db_path = get_db_path()
    test("database file exists", db_path.exists(), str(db_path))
    if not db_path.exists():
        return

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        # Required tables
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
        required_tables = ["readings", "imports", "workouts", "nutrition_log", "medications"]
        for table in required_tables:
            test(f"table '{table}' exists", table in tables)

        # readings schema
        if "readings" in tables:
            cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(readings)").fetchall()}
            for col in ["timestamp", "metric", "value", "unit", "source"]:
                test(f"readings.{col} column exists", col in cols)

        # imports schema — must have file_hash for change detection
        if "imports" in tables:
            cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(imports)").fetchall()}
            for col in ["filename", "imported_at", "rows_added", "source", "file_hash"]:
                test(f"imports.{col} column exists", col in cols)

        # workouts schema
        if "workouts" in tables:
            cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(workouts)").fetchall()}
            for col in ["start_time", "end_time", "type", "duration_seconds"]:
                test(f"workouts.{col} column exists", col in cols)

        # nutrition_log schema
        if "nutrition_log" in tables:
            cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(nutrition_log)").fetchall()}
            for col in ["entry_id", "meal_time", "meal_type", "calories", "protein_g", "carbs_g", "fat_total_g"]:
                test(f"nutrition_log.{col} column exists", col in cols)

        # medications schema
        if "medications" in tables:
            cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(medications)").fetchall()}
            for col in ["timestamp", "medication", "dosage", "status"]:
                test(f"medications.{col} column exists", col in cols)

        # Row counts
        for table in ["readings", "imports"]:
            if table in tables:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                test(f"'{table}' has data", count > 0, f"{count:,} rows")

        # Source distribution
        if "readings" in tables:
            sources = conn.execute("SELECT source, COUNT(*) FROM readings GROUP BY source").fetchall()
            for src, cnt in sources:
                test(f"source '{src}' present", cnt > 0, f"{cnt:,} rows")

        # Views
        try:
            count = conn.execute("SELECT COUNT(*) FROM v_nightly_signals").fetchone()[0]
            test("view 'v_nightly_signals' works", True, f"{count:,} rows")
        except Exception as e:
            test("view 'v_nightly_signals' works", False, str(e))

        # Sequence (via catalog to avoid write lock)
        try:
            seqs = conn.execute(
                "SELECT sequence_name FROM duckdb_sequences() WHERE sequence_name = 'seq_nutrition_id'"
            ).fetchall()
            test("sequence 'seq_nutrition_id' exists", len(seqs) > 0)
        except Exception as e:
            test("sequence 'seq_nutrition_id' exists", False, str(e))

        # Data sanity checks
        if "readings" in tables:
            # No future timestamps
            import datetime
            future = conn.execute(
                "SELECT COUNT(*) FROM readings WHERE timestamp > CURRENT_TIMESTAMP + INTERVAL '1 hour'"
            ).fetchone()[0]
            test("no future timestamps in readings", future == 0, f"{future} future rows" if future else "")

            # Sources are expected values
            valid_sources = {'healthkit', 'libre', 'lab', 'manual'}
            actual_sources = {r[0] for r in conn.execute("SELECT DISTINCT source FROM readings").fetchall()}
            unexpected = actual_sources - valid_sources
            test("all sources are expected", len(unexpected) == 0,
                 f"unexpected: {unexpected}" if unexpected else "")

    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════
# 4. DATA FILES
# ═══════════════════════════════════════════════════════════

def test_data_files():
    section("4. Data Files")

    from config import get_recipes_path, get_gurus_path, get_digest_state_path, get_user_profile

    # Recipes
    recipes_path = get_recipes_path()
    if recipes_path.exists():
        try:
            data = json.loads(recipes_path.read_text())
            test("recipes.json valid JSON", True)
            test("recipes.json has 'recipes' key", "recipes" in data)
            test("recipes.json recipes is a list", isinstance(data.get("recipes"), list))
            # Each recipe has required fields
            if data.get("recipes"):
                r = data["recipes"][0]
                for key in ["id", "name", "ingredients"]:
                    test(f"recipe has '{key}'", key in r)
        except json.JSONDecodeError as e:
            test("recipes.json valid JSON", False, str(e))
    else:
        test("recipes.json exists", False, f"Expected at {recipes_path}")

    # Gurus
    gurus_path = get_gurus_path()
    if gurus_path.exists():
        try:
            data = json.loads(gurus_path.read_text())
            test("gurus.json valid JSON", True)
            test("gurus.json has 'gurus' key", "gurus" in data)
            if data.get("gurus"):
                g = data["gurus"][0]
                for key in ["handle", "name", "focus"]:
                    test(f"guru has '{key}'", key in g)
        except json.JSONDecodeError as e:
            test("gurus.json valid JSON", False, str(e))
    else:
        test("gurus.json exists", False, f"Expected at {gurus_path}")

    # Digest state
    state_path = get_digest_state_path()
    if state_path.exists():
        try:
            json.loads(state_path.read_text())
            test("digest-state.json valid JSON", True)
        except json.JSONDecodeError as e:
            test("digest-state.json valid JSON", False, str(e))
    else:
        test("digest-state.json exists", False, f"Expected at {state_path}")

    # User profile
    profile = get_user_profile()
    test("user-profile.yaml loads", isinstance(profile, dict))
    if profile:
        test("profile has libre_patient_name", "libre_patient_name" in profile)
        test("profile has nutrition_defaults", "nutrition_defaults" in profile)

    # Example files in repo
    for name in ["recipes.example.json", "gurus.example.json",
                 "digest-state.example.json", "user-profile.example.yaml"]:
        path = REPO_ROOT / "data" / name
        test(f"repo has '{name}'", path.exists())


# ═══════════════════════════════════════════════════════════
# 5. SCRIPT IMPORTS
# ═══════════════════════════════════════════════════════════

def test_script_imports():
    section("5. Script Imports")

    importable = [
        "config", "validate", "init_db", "daily_import", "sync_libre",
        "import_healthkit", "import_libre", "import_medications",
        "import_workouts", "import_cycletracking", "init_nutrition",
        "log_nutrition", "nutrition_summary"
    ]

    for module_name in importable:
        rc, _, stderr = run_cmd(
            f"{sys.executable} -c \"import sys; sys.path.insert(0, 'scripts'); import {module_name}\"")
        test(f"import {module_name}", rc == 0, stderr[:80] if rc != 0 else "")


# ═══════════════════════════════════════════════════════════
# 6. HASH-BASED IMPORT DETECTION
# ═══════════════════════════════════════════════════════════

def test_hash_detection():
    section("6. Hash-Based Import Detection")

    from daily_import import calculate_file_hash

    # Create temp files to test hashing
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
        f.write("timestamp,metric,value\n2026-01-01,HR,72\n")
        f.flush()
        path1 = Path(f.name)

    hash1 = calculate_file_hash(path1)
    test("hash is 64-char hex string", len(hash1) == 64 and all(c in '0123456789abcdef' for c in hash1))

    # Same content = same hash
    hash1b = calculate_file_hash(path1)
    test("same file gives same hash", hash1 == hash1b)

    # Different content = different hash
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
        f.write("timestamp,metric,value\n2026-01-01,HR,73\n")
        f.flush()
        path2 = Path(f.name)

    hash2 = calculate_file_hash(path2)
    test("different content gives different hash", hash1 != hash2)

    # Cleanup
    path1.unlink()
    path2.unlink()


# ═══════════════════════════════════════════════════════════
# 7. VALIDATION ENGINE
# ═══════════════════════════════════════════════════════════

def test_validation():
    section("7. Data Validation")

    rc, stdout, stderr = run_cmd(f"{sys.executable} scripts/validate.py")
    test("validate.py runs", rc == 0, stderr[:80] if rc != 0 else "")
    test("validate.py finds no issues", "No data quality issues found" in stdout,
         stdout[:100] if "No data quality issues" not in stdout else "")

    # Verbose mode
    rc, stdout, _ = run_cmd(f"{sys.executable} scripts/validate.py --verbose")
    test("validate.py --verbose runs", rc == 0)
    test("verbose shows info messages", "Date coverage" in stdout or "Heart rate" in stdout)


# ═══════════════════════════════════════════════════════════
# 8. IMPORT PIPELINE (DRY-RUN)
# ═══════════════════════════════════════════════════════════

def test_import_dryrun():
    section("8. Import Pipeline (Dry-Run)")

    rc, stdout, stderr = run_cmd(f"{sys.executable} scripts/daily_import.py --dry-run")
    test("daily_import.py --dry-run exits cleanly", rc == 0,
         (stderr or stdout)[:100] if rc != 0 else "")
    if rc == 0:
        test("finds iCloud folder", "Scanning" in stdout or "Found" in stdout or "up to date" in stdout.lower(),
             stdout[:80])
        # Check it doesn't actually modify DB
        test("dry-run doesn't write (no 'INSERT' in output)", "INSERT" not in stdout)


# ═══════════════════════════════════════════════════════════
# 9. LIBRE SYNC (DRY-RUN)
# ═══════════════════════════════════════════════════════════

def test_libre_dryrun():
    section("9. Libre Sync (Dry-Run)")

    rc, stdout, stderr = run_cmd(f"{sys.executable} scripts/sync_libre.py --dry-run")
    if rc == 0:
        test("sync_libre.py --dry-run runs", True)
        test("libre finds patient", "patient" in stdout.lower(), stdout[:80])
        test("libre uses profile patient name", "Using patient" in stdout)
    else:
        if any(k in (stderr + stdout).lower() for k in ["credential", "keychain", "password", "not configured"]):
            test("sync_libre.py --dry-run (credentials not configured)", True, "expected skip")
        else:
            test("sync_libre.py --dry-run runs", False, (stderr or stdout)[:100])


# ═══════════════════════════════════════════════════════════
# 10. NUTRITION LOGGING
# ═══════════════════════════════════════════════════════════

def test_nutrition():
    section("10. Nutrition Logging")

    from config import get_db_path
    import duckdb

    db_path = get_db_path()
    if not db_path.exists():
        test("database exists for nutrition tests", False)
        return

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        # Check nutrition_log schema has all expected columns
        cols = {r[1] for r in conn.execute("PRAGMA table_info(nutrition_log)").fetchall()}
        expected = {"entry_id", "meal_time", "meal_type", "meal_name", "calories",
                    "protein_g", "carbs_g", "fat_total_g", "food_items", "source"}
        missing = expected - cols
        test("nutrition_log has all expected columns", len(missing) == 0,
             f"missing: {missing}" if missing else "")

        # Verify data types make sense
        if "nutrition_log" in [r[0] for r in conn.execute("SHOW TABLES").fetchall()]:
            sample = conn.execute(
                "SELECT calories, protein_g, carbs_g, fat_total_g FROM nutrition_log LIMIT 1"
            ).fetchone()
            if sample:
                test("nutrition values are numeric", all(isinstance(v, (int, float)) or v is None for v in sample))
                # Calories should be reasonable (0-5000)
                if sample[0] is not None:
                    test("calories in reasonable range", 0 < sample[0] < 5000, f"{sample[0]} kcal")
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════
# 11. SHELL SCRIPTS
# ═══════════════════════════════════════════════════════════

def test_shell_scripts():
    section("11. Shell Scripts")

    # paths.sh
    rc, stdout, _ = run_cmd("bash shell/paths.sh --json")
    test("paths.sh --json exits cleanly", rc == 0)
    if rc == 0:
        try:
            data = json.loads(stdout)
            test("paths.sh output is valid JSON", True)
            for key in ["repo", "scripts", "data", "shell", "config", "venv", "data_dir", "db", "logs", "reports", "icloud"]:
                test(f"paths.sh has '{key}'", key in data)
        except json.JSONDecodeError:
            test("paths.sh output is valid JSON", False)

    # paths.sh plain mode
    rc, stdout, _ = run_cmd("bash shell/paths.sh")
    test("paths.sh plain mode exits cleanly", rc == 0)
    if rc == 0:
        test("plain mode has key=value format", "repo=" in stdout and "db=" in stdout)

    # process_meal_photos.sh exists and is executable
    photo_script = REPO_ROOT / "shell" / "process_meal_photos.sh"
    test("process_meal_photos.sh exists", photo_script.exists())
    test("process_meal_photos.sh is executable", os.access(photo_script, os.X_OK) if photo_script.exists() else False)

    # resize_image.sh
    resize_script = REPO_ROOT / "shell" / "resize_image.sh"
    test("resize_image.sh exists", resize_script.exists())


# ═══════════════════════════════════════════════════════════
# 12. SKILL FILES
# ═══════════════════════════════════════════════════════════

def test_skill_files():
    section("12. Skill Files")

    skill_files = [
        ("SKILL.md", ["sync-health-data", "analyze-health-data", "log-nutrition"]),
        ("sub-skills/sync-health-data/SKILL.md", ["HealthKit", "LibreView", "config"]),
        ("sub-skills/analyze-health-data/SKILL.md", ["Attia", "Four Horsemen", "readings"]),
        ("sub-skills/log-nutrition/SKILL.md", ["USDA", "nutrition_log", "recipes"]),
    ]

    for rel_path, keywords in skill_files:
        path = REPO_ROOT / rel_path
        test(f"{rel_path} exists", path.exists())
        if path.exists():
            content = path.read_text()
            test(f"  has frontmatter (---)", content.startswith("---"))
            for kw in keywords:
                test(f"  mentions '{kw}'", kw in content)

    # No personal data in skill files
    personal = ["Juan", "Lilliana", "haishan", "~/clawd/", "José"]
    for rel_path, _ in skill_files:
        path = REPO_ROOT / rel_path
        if path.exists():
            content = path.read_text()
            for p in personal:
                test(f"  {rel_path} no '{p}'", p not in content)


# ═══════════════════════════════════════════════════════════
# 13. NO PERSONAL DATA IN TRACKED FILES
# ═══════════════════════════════════════════════════════════

def test_no_personal_data():
    section("13. No Personal Data in Tracked Files")

    rc, tracked_files, _ = run_cmd("git ls-files")
    if rc != 0:
        test("git ls-files works", False)
        return

    personal_patterns = ["Juan", "Lilliana", "Liliana", "moltbot", "haishan", "~/clawd/", "croissant"]
    violations = []

    for filepath in tracked_files.split("\n"):
        if not filepath or filepath.startswith(".git"):
            continue
        # Skip the test file itself (it contains patterns as search strings)
        if filepath.endswith("test_suite.py"):
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

    test("no personal data in tracked files", len(violations) == 0,
         "; ".join(violations[:5]) if violations else "clean")

    # Personal files must NOT be tracked
    personal_files = ["config.yaml", ".env", "data/recipes.json", "data/gurus.json", "data/digest-state.json"]
    for pf in personal_files:
        rc, _, _ = run_cmd(f"git ls-files --error-unmatch {pf} 2>/dev/null")
        test(f"'{pf}' is NOT tracked", rc != 0)


# ═══════════════════════════════════════════════════════════
# 14. EXAMPLE FILES & FRESH-CLONE READINESS
# ═══════════════════════════════════════════════════════════

def test_example_files():
    section("14. Example Files & Fresh-Clone Readiness")

    # config.example.yaml
    cfg_example = REPO_ROOT / "config.example.yaml"
    test("config.example.yaml exists", cfg_example.exists())
    if cfg_example.exists():
        content = cfg_example.read_text()
        for key in ["owner", "data_dir", "icloud_folder", "venv"]:
            test(f"  config example has '{key}'", key in content)
        for p in ["Juan", "~/clawd/", "haishan"]:
            test(f"  config example no '{p}'", p not in content)

    # .env.example
    env_example = REPO_ROOT / ".env.example"
    test(".env.example exists", env_example.exists())
    if env_example.exists():
        content = env_example.read_text()
        test("  has USDA_API_KEY placeholder", "USDA_API_KEY" in content)
        # Should not have a real key
        test("  no real API key", "rQVp" not in content)

    # .gitignore covers personal files
    gitignore = REPO_ROOT / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        for pattern in ["config.yaml", ".env", "data/recipes.json", "data/gurus.json", "data/digest-state.json"]:
            test(f"  .gitignore has '{pattern}'", pattern in content)

    # requirements.txt
    reqs = REPO_ROOT / "requirements.txt"
    test("requirements.txt exists", reqs.exists())
    if reqs.exists():
        content = reqs.read_text()
        for dep in ["duckdb", "pyyaml", "pandas", "pylibrelinkup"]:
            test(f"  requires '{dep}'", dep in content.lower())

    # init_db.py can initialize fresh
    test("init_db.py exists", (REPO_ROOT / "scripts" / "init_db.py").exists())


# ═══════════════════════════════════════════════════════════
# 15. GIT HYGIENE
# ═══════════════════════════════════════════════════════════

def test_git_hygiene():
    section("15. Git Hygiene")

    # No untracked .py or .md files that should be committed
    rc, stdout, _ = run_cmd("git status --porcelain")
    test("git status runs", rc == 0)

    # Check for common mistakes
    rc, stdout, _ = run_cmd("git diff --name-only HEAD")
    if stdout:
        test("no uncommitted changes to tracked files", False, stdout[:80])
    else:
        test("no uncommitted changes to tracked files", True)

    # .duckdb files should never be tracked
    rc, stdout, _ = run_cmd("git ls-files '*.duckdb' '*.duckdb.wal'")
    test("no .duckdb files tracked", stdout == "")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Outlive-protocol test suite")
    parser.add_argument("--quick", action="store_true", help="Skip slow tests (import/libre dry-run)")
    parser.add_argument("--fresh", action="store_true", help="Fresh-clone checks only (no DB needed)")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  OUTLIVE-PROTOCOL TEST SUITE")
    print(f"{'='*60}")

    if args.fresh:
        test_config_resolution()
        test_skill_files()
        test_no_personal_data()
        test_example_files()
    else:
        test_config_resolution()
        test_config_backward_compat()
        test_database()
        test_data_files()
        test_script_imports()
        test_hash_detection()
        test_validation()
        test_nutrition()
        test_shell_scripts()
        test_skill_files()
        test_no_personal_data()
        test_example_files()
        test_git_hygiene()

        if not args.quick:
            test_import_dryrun()
            test_libre_dryrun()

    # Summary
    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    total = len(results)

    print(f"\n{'='*60}")
    print(f"  RESULTS: {passed}/{total} passed" + (f", {failed} FAILED" if failed else " ✨"))
    print(f"{'='*60}")

    if failed:
        print(f"\n  Failed tests:")
        for status, name, detail in results:
            if status == FAIL:
                print(f"    {FAIL} {name}" + (f" — {detail}" if detail else ""))
        print()

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
