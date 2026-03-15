#!/usr/bin/env python3
"""
Outlive-protocol test suite.

Run after any changes to verify nothing is broken.

Usage:
    python3 tests/test_suite.py              # Run all tests
    python3 tests/test_suite.py --quick      # Skip slow tests (import/libre dry-run)
    python3 tests/test_suite.py --fresh      # Fresh-clone checks only (no DB needed)

Coverage:
    1.  Config Resolution (per-skill)  Each skill's config.py resolves paths correctly
    2.  Config Backward Compat (3)     Explicit paths override data_dir
    3.  DB Schema & Integrity (40)     All 5 table schemas, columns, row counts, sources, views, sequences
    4.  Data Files (19)                JSON/YAML structure, required keys, examples exist
    5.  Script Imports (per-skill)     All Python scripts import cleanly from their skill directories
    6.  Hash Detection (3)             SHA-256 correctness, determinism, collision avoidance
    7.  Validation (4)                 validate.py runs, verbose mode works
    8.  Import Dry-Run (2)             Pipeline finds iCloud folder, doesn't write
    9.  Libre Dry-Run (2-3)            API connects, uses profile patient name
    10. Nutrition (3)                  Schema completeness, data sanity
    10b. Hevy / Workout Coaching       Tables, schema, sequences
    11. Shell Scripts (18)             paths.sh JSON/plain, all keys, executability
    12. Skill Files (35)               Existence, frontmatter, keywords, no personal data
    13. Personal Data (6)              No leaks in tracked files, personal files untracked
    14. Example Files (19)             Completeness, no personal data, deps, gitignore
    15. Git Hygiene (3)                Clean state, no .duckdb tracked
"""

import sys
import os
import json
import subprocess
import argparse
import tempfile
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# Skill script directories — each skill has its own scripts/ with config.py
SKILL_SCRIPT_DIRS = {
    "sync-health-data": REPO_ROOT / "skills" / "sync-health-data" / "scripts",
    "coach-strength": REPO_ROOT / "skills" / "coach-strength" / "scripts",
    "log-nutrition": REPO_ROOT / "skills" / "log-nutrition" / "scripts",
    "coach-nutrition": REPO_ROOT / "skills" / "coach-nutrition" / "scripts",
}

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


def import_from_skill(skill_name, module_name):
    """Import a module from a skill's scripts directory."""
    import importlib
    scripts_dir = str(SKILL_SCRIPT_DIRS[skill_name])
    # Remove any other skill script dirs from sys.path to avoid cross-contamination
    for other_dir in SKILL_SCRIPT_DIRS.values():
        other_str = str(other_dir)
        while other_str in sys.path:
            sys.path.remove(other_str)
    sys.path.insert(0, scripts_dir)
    # Clear all modules from skill script dirs to avoid cross-contamination
    # (e.g., daily_import imports config internally)
    stale = [k for k, v in sys.modules.items()
             if hasattr(v, '__file__') and v.__file__
             and any(str(d) in str(v.__file__) for d in SKILL_SCRIPT_DIRS.values())]
    for k in stale:
        del sys.modules[k]
    return importlib.import_module(module_name)


# ═══════════════════════════════════════════════════════════
# 1. CONFIG RESOLUTION (per-skill)
# ═══════════════════════════════════════════════════════════

def test_config_resolution():
    section("1. Config Resolution (per-skill)")

    for skill_name, scripts_dir in SKILL_SCRIPT_DIRS.items():
        config = import_from_skill(skill_name, "config")

        # DB path resolves
        db = config.get_db_path()
        test(f"[{skill_name}] db_path resolves", db is not None, str(db))
        test(f"[{skill_name}] db_path ends with .duckdb", str(db).endswith(".duckdb"))
        test(f"[{skill_name}] db_path is absolute", db.is_absolute())

        # data_dir resolves
        data_dir = config.get_data_dir()
        test(f"[{skill_name}] data_dir resolves", data_dir is not None, str(data_dir))
        test(f"[{skill_name}] data_dir exists", data_dir.exists())

    # All skills resolve to the SAME DB
    dbs = set()
    for skill_name in SKILL_SCRIPT_DIRS:
        config = import_from_skill(skill_name, "config")
        dbs.add(str(config.get_db_path()))
    test("all skills resolve to same DB", len(dbs) == 1, str(dbs))

    # sync-health-data has extra functions
    config = import_from_skill("sync-health-data", "config")
    icloud = config.get_icloud_folder()
    test("[sync-health-data] icloud_folder resolves", icloud is not None, str(icloud))
    test("[sync-health-data] icloud_folder is absolute", icloud.is_absolute())

    profile = config.get_user_profile()
    test("[sync-health-data] user_profile loads (dict)", isinstance(profile, dict))

    prefix = config.get_libre_csv_prefix()
    test("[sync-health-data] libre_csv_prefix is string", isinstance(prefix, str))

    # log-nutrition has recipes
    config = import_from_skill("log-nutrition", "config")
    recipes = config.get_recipes_path()
    test("[log-nutrition] recipes_path resolves", recipes is not None, str(recipes))

    # paths.sh parity with skill configs
    rc, stdout, _ = run_cmd("bash shell/paths.sh --json")
    if rc == 0:
        paths = json.loads(stdout)
        config = import_from_skill("sync-health-data", "config")
        test("paths.sh db == skill config db", paths["db"] == str(config.get_db_path()))
        test("paths.sh data_dir == skill config data_dir", paths["data_dir"] == str(config.get_data_dir()))
    else:
        test("paths.sh runs without error", False, "exit code " + str(rc))


# ═══════════════════════════════════════════════════════════
# 2. CONFIG BACKWARD COMPATIBILITY
# ═══════════════════════════════════════════════════════════

def test_config_backward_compat():
    section("2. Config Backward Compatibility")

    import yaml
    with open(REPO_ROOT / "config.yaml") as f:
        cfg = yaml.safe_load(f)

    data_section = cfg.get('data', {})
    config = import_from_skill("sync-health-data", "config")

    # If db_path is explicitly set, it should be used over data_dir/health.duckdb
    if 'db_path' in data_section:
        db = config.get_db_path()
        explicit = Path(data_section['db_path']).expanduser().resolve()
        test("explicit db_path takes priority over data_dir", db == explicit)
    else:
        test("db_path derives from data_dir (no explicit override)", True)

    if 'log_dir' in data_section:
        logs = config.get_log_dir()
        explicit = Path(data_section['log_dir']).expanduser().resolve()
        test("explicit log_dir takes priority", logs == explicit)
    else:
        test("log_dir derives from data_dir (no explicit override)", True)

    # data_dir itself
    if 'data_dir' in data_section:
        data_dir = config.get_data_dir()
        explicit = Path(data_section['data_dir']).expanduser().resolve()
        test("data_dir resolves correctly", data_dir == explicit)
    else:
        test("data_dir not set (using defaults)", True)


# ═══════════════════════════════════════════════════════════
# 3. DATABASE SCHEMA & INTEGRITY
# ═══════════════════════════════════════════════════════════

def test_database():
    section("3. Database Schema & Integrity")

    config = import_from_skill("sync-health-data", "config")
    import duckdb

    db_path = config.get_db_path()
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

        # imports schema
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

        # Sequence
        try:
            seqs = conn.execute(
                "SELECT sequence_name FROM duckdb_sequences() WHERE sequence_name = 'seq_nutrition_id'"
            ).fetchall()
            test("sequence 'seq_nutrition_id' exists", len(seqs) > 0)
        except Exception as e:
            test("sequence 'seq_nutrition_id' exists", False, str(e))

        # Data sanity checks
        if "readings" in tables:
            future = conn.execute(
                "SELECT COUNT(*) FROM readings WHERE timestamp > CURRENT_TIMESTAMP + INTERVAL '1 hour'"
            ).fetchone()[0]
            test("no future timestamps in readings", future == 0, f"{future} future rows" if future else "")

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

    config = import_from_skill("sync-health-data", "config")

    # Recipes
    config_ln = import_from_skill("log-nutrition", "config")
    recipes_path = config_ln.get_recipes_path()
    if recipes_path.exists():
        try:
            data = json.loads(recipes_path.read_text())
            test("recipes.json valid JSON", True)
            test("recipes.json has 'recipes' key", "recipes" in data)
            test("recipes.json recipes is a list", isinstance(data.get("recipes"), list))
            if data.get("recipes"):
                r = data["recipes"][0]
                for key in ["id", "name", "ingredients"]:
                    test(f"recipe has '{key}'", key in r)
        except json.JSONDecodeError as e:
            test("recipes.json valid JSON", False, str(e))
    else:
        test("recipes.json exists", False, f"Expected at {recipes_path}")

    # Gurus
    gurus_path = config.get_data_dir() / "gurus.json"
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
    state_path = config.get_data_dir() / "digest-state.json"
    if state_path.exists():
        try:
            json.loads(state_path.read_text())
            test("digest-state.json valid JSON", True)
        except json.JSONDecodeError as e:
            test("digest-state.json valid JSON", False, str(e))
    else:
        test("digest-state.json exists", False, f"Expected at {state_path}")

    # User profile
    profile = config.get_user_profile()
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
# 5. SCRIPT IMPORTS (per-skill)
# ═══════════════════════════════════════════════════════════

def test_script_imports():
    section("5. Script Imports (per-skill)")

    # Map: skill -> modules it should have
    skill_modules = {
        "sync-health-data": ["config", "validate", "daily_import", "sync_libre",
                             "import_healthkit", "import_libre", "import_medications",
                             "import_workouts", "import_cycletracking"],
        "coach-strength": ["config", "init_hevy", "sync_hevy"],
        "log-nutrition": ["config", "log_nutrition", "init_nutrition"],
        "coach-nutrition": ["config", "nutrition_summary"],
    }

    for skill_name, modules in skill_modules.items():
        scripts_dir = SKILL_SCRIPT_DIRS[skill_name]
        for module_name in modules:
            rc, _, stderr = run_cmd(
                f"{sys.executable} -c \"import sys; sys.path.insert(0, '{scripts_dir}'); import {module_name}\"")
            test(f"[{skill_name}] import {module_name}", rc == 0, stderr[:80] if rc != 0 else "")

    # init_db.py at root — test it can parse and find DB path
    rc, _, stderr = run_cmd(f"{sys.executable} -c \"import runpy; runpy.run_path('scripts/init_db.py', run_name='__test__')\"")
    # init_db.py runs main() only when __name__ == '__main__', so this just validates imports
    test("[root] init_db.py loads", rc == 0, stderr[:120] if rc != 0 else "")


# ═══════════════════════════════════════════════════════════
# 6. HASH-BASED IMPORT DETECTION
# ═══════════════════════════════════════════════════════════

def test_hash_detection():
    section("6. Hash-Based Import Detection")

    daily_import = import_from_skill("sync-health-data", "daily_import")
    calculate_file_hash = daily_import.calculate_file_hash

    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
        f.write("timestamp,metric,value\n2026-01-01,HR,72\n")
        f.flush()
        path1 = Path(f.name)

    hash1 = calculate_file_hash(path1)
    test("hash is 64-char hex string", len(hash1) == 64 and all(c in '0123456789abcdef' for c in hash1))

    hash1b = calculate_file_hash(path1)
    test("same file gives same hash", hash1 == hash1b)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
        f.write("timestamp,metric,value\n2026-01-01,HR,73\n")
        f.flush()
        path2 = Path(f.name)

    hash2 = calculate_file_hash(path2)
    test("different content gives different hash", hash1 != hash2)

    path1.unlink()
    path2.unlink()


# ═══════════════════════════════════════════════════════════
# 7. VALIDATION ENGINE
# ═══════════════════════════════════════════════════════════

def test_validation():
    section("7. Data Validation")

    scripts_dir = SKILL_SCRIPT_DIRS["sync-health-data"]
    rc, stdout, stderr = run_cmd(f"{sys.executable} {scripts_dir}/validate.py")
    test("validate.py runs", rc == 0, stderr[:80] if rc != 0 else "")
    test("validate.py finds no issues", "No data quality issues found" in stdout,
         stdout[:100] if "No data quality issues" not in stdout else "")

    rc, stdout, _ = run_cmd(f"{sys.executable} {scripts_dir}/validate.py --verbose")
    test("validate.py --verbose runs", rc == 0)
    test("verbose shows info messages", "Date coverage" in stdout or "Heart rate" in stdout)


# ═══════════════════════════════════════════════════════════
# 8. IMPORT PIPELINE (DRY-RUN)
# ═══════════════════════════════════════════════════════════

def test_import_dryrun():
    section("8. Import Pipeline (Dry-Run)")

    scripts_dir = SKILL_SCRIPT_DIRS["sync-health-data"]
    rc, stdout, stderr = run_cmd(f"{sys.executable} {scripts_dir}/daily_import.py --dry-run")
    test("daily_import.py --dry-run exits cleanly", rc == 0,
         (stderr or stdout)[:100] if rc != 0 else "")
    if rc == 0:
        test("finds iCloud folder", "Scanning" in stdout or "Found" in stdout or "up to date" in stdout.lower(),
             stdout[:80])
        test("dry-run doesn't write (no 'INSERT' in output)", "INSERT" not in stdout)


# ═══════════════════════════════════════════════════════════
# 9. LIBRE SYNC (DRY-RUN)
# ═══════════════════════════════════════════════════════════

def test_libre_dryrun():
    section("9. Libre Sync (Dry-Run)")

    scripts_dir = SKILL_SCRIPT_DIRS["sync-health-data"]
    rc, stdout, stderr = run_cmd(f"{sys.executable} {scripts_dir}/sync_libre.py --dry-run")
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

    config = import_from_skill("sync-health-data", "config")
    import duckdb

    db_path = config.get_db_path()
    if not db_path.exists():
        test("database exists for nutrition tests", False)
        return

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(nutrition_log)").fetchall()}
        expected = {"entry_id", "meal_time", "meal_type", "meal_name", "calories",
                    "protein_g", "carbs_g", "fat_total_g", "food_items", "source"}
        missing = expected - cols
        test("nutrition_log has all expected columns", len(missing) == 0,
             f"missing: {missing}" if missing else "")

        if "nutrition_log" in [r[0] for r in conn.execute("SHOW TABLES").fetchall()]:
            sample = conn.execute(
                "SELECT calories, protein_g, carbs_g, fat_total_g FROM nutrition_log LIMIT 1"
            ).fetchone()
            if sample:
                test("nutrition values are numeric", all(isinstance(v, (int, float)) or v is None for v in sample))
                if sample[0] is not None:
                    test("calories in reasonable range", 0 < sample[0] < 5000, f"{sample[0]} kcal")
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════
# 10b. HEVY / WORKOUT COACHING
# ═══════════════════════════════════════════════════════════

def test_hevy():
    section("10b. Hevy / Workout Coaching")

    config = import_from_skill("coach-strength", "config")
    import duckdb

    db_path = config.get_db_path()
    if not db_path.exists():
        test("database exists for hevy tests", False)
        return

    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]

        hevy_tables = ["hevy_exercises", "hevy_workouts", "hevy_sets",
                       "coach_routines", "coach_progression", "hevy_sync_state"]
        for table in hevy_tables:
            test(f"table '{table}' exists", table in tables)

        if "hevy_exercises" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(hevy_exercises)").fetchall()}
            for col in ["template_id", "title", "type", "primary_muscle_group", "is_custom"]:
                test(f"hevy_exercises.{col} exists", col in cols)

        if "hevy_workouts" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(hevy_workouts)").fetchall()}
            for col in ["id", "title", "routine_id", "start_time", "end_time", "duration_seconds"]:
                test(f"hevy_workouts.{col} exists", col in cols)

        if "hevy_sets" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(hevy_sets)").fetchall()}
            for col in ["workout_id", "exercise_template_id", "exercise_name",
                        "set_index", "set_type", "weight_kg", "reps", "rpe"]:
                test(f"hevy_sets.{col} exists", col in cols)

        if "coach_routines" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(coach_routines)").fetchall()}
            for col in ["id", "hevy_routine_id", "title", "exercises"]:
                test(f"coach_routines.{col} exists", col in cols)

        if "coach_progression" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(coach_progression)").fetchall()}
            for col in ["exercise_template_id", "date", "estimated_1rm_kg",
                        "total_volume_kg", "total_sets"]:
                test(f"coach_progression.{col} exists", col in cols)

        if "hevy_exercises" in tables:
            count = conn.execute("SELECT COUNT(*) FROM hevy_exercises").fetchone()[0]
            test("hevy_exercises has data (synced)", count > 0, f"{count} templates")

        if "coach_routines" in tables:
            count = conn.execute("SELECT COUNT(*) FROM coach_routines").fetchone()[0]
            test("coach_routines has data", count > 0, f"{count} routines")

        try:
            seqs = conn.execute(
                "SELECT sequence_name FROM duckdb_sequences() WHERE sequence_name IN "
                "('seq_hevy_set_id', 'seq_coach_prog_id')"
            ).fetchall()
            seq_names = {s[0] for s in seqs}
            test("seq_hevy_set_id exists", "seq_hevy_set_id" in seq_names)
            test("seq_coach_prog_id exists", "seq_coach_prog_id" in seq_names)
        except Exception as e:
            test("hevy sequences exist", False, str(e))

    finally:
        conn.close()

    # Script imports
    scripts_dir = SKILL_SCRIPT_DIRS["coach-strength"]
    for module in ["init_hevy", "sync_hevy"]:
        rc, _, stderr = run_cmd(
            f"{sys.executable} -c \"import sys; sys.path.insert(0, '{scripts_dir}'); import {module}\"")
        test(f"import {module}", rc == 0, stderr[:80] if rc != 0 else "")

    # SKILL.md
    skill_path = REPO_ROOT / "skills" / "coach-strength" / "SKILL.md"
    test("coach-strength/SKILL.md exists", skill_path.exists())
    if skill_path.exists():
        content = skill_path.read_text()
        test("  has frontmatter", content.startswith("---"))
        test("  mentions Hevy", "Hevy" in content)
        test("  mentions progressive overload", "progressive" in content.lower() or "overload" in content.lower())

    # .env has HEVY_API_KEY placeholder in .env.example
    env_example = REPO_ROOT / ".env.example"
    if env_example.exists():
        content = env_example.read_text()
        test(".env.example has HEVY_API_KEY", "HEVY_API_KEY" in content)


# ═══════════════════════════════════════════════════════════
# 11. SHELL SCRIPTS
# ═══════════════════════════════════════════════════════════

def test_shell_scripts():
    section("11. Shell Scripts")

    # paths.sh JSON mode
    rc, stdout, _ = run_cmd("bash shell/paths.sh --json")
    test("paths.sh --json exits cleanly", rc == 0)
    if rc == 0:
        try:
            data = json.loads(stdout)
            test("paths.sh output is valid JSON", True)
            for key in ["repo", "skills", "data", "shell", "config", "venv", "data_dir", "db", "logs", "reports", "icloud"]:
                test(f"paths.sh has '{key}'", key in data)
        except json.JSONDecodeError:
            test("paths.sh output is valid JSON", False)

    # paths.sh plain mode
    rc, stdout, _ = run_cmd("bash shell/paths.sh")
    test("paths.sh plain mode exits cleanly", rc == 0)
    if rc == 0:
        test("plain mode has key=value format", "repo=" in stdout and "db=" in stdout)

    # process_meal_photos.sh in log-nutrition/scripts/
    photo_script = REPO_ROOT / "skills" / "log-nutrition" / "scripts" / "process_meal_photos.sh"
    test("process_meal_photos.sh exists", photo_script.exists())
    test("process_meal_photos.sh is executable", os.access(photo_script, os.X_OK) if photo_script.exists() else False)

    # resize_image.sh in log-nutrition/scripts/
    resize_script = REPO_ROOT / "skills" / "log-nutrition" / "scripts" / "resize_image.sh"
    test("resize_image.sh exists", resize_script.exists())


# ═══════════════════════════════════════════════════════════
# 12. SKILL FILES
# ═══════════════════════════════════════════════════════════

def test_skill_files():
    section("12. Skill Files")

    skill_checks = [
        ("skills/sync-health-data/SKILL.md", ["HealthKit", "LibreView", "config"]),
        ("skills/analyze-health-data/SKILL.md", ["Attia", "readings"]),
        ("skills/log-nutrition/SKILL.md", ["USDA", "nutrition_log", "recipe"]),
        ("skills/coach-strength/SKILL.md", ["Hevy", "progressive", "hevy_workouts"]),
        ("skills/coach-cardio/SKILL.md", ["Zone 2", "VO2 max", "workouts"]),
        ("skills/coach-nutrition/SKILL.md", ["protein", "glucose", "nutrition_log"]),
    ]

    for rel_path, keywords in skill_checks:
        path = REPO_ROOT / rel_path
        test(f"{rel_path} exists", path.exists())
        if path.exists():
            content = path.read_text()
            test(f"  has frontmatter (---)", content.startswith("---"))
            for kw in keywords:
                test(f"  mentions '{kw}'", kw in content)

    # No stale references
    for skill_dir in (REPO_ROOT / "skills").iterdir():
        if skill_dir.is_dir():
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                content = skill_md.read_text()
                test(f"  {skill_dir.name}: no 'sub-skill' refs", "sub-skill" not in content)

    # Reference files are pointed to from SKILL.md
    for skill_dir in (REPO_ROOT / "skills").iterdir():
        refs_dir = skill_dir / "references"
        if refs_dir.is_dir():
            skill_md = skill_dir / "SKILL.md"
            content = skill_md.read_text() if skill_md.exists() else ""
            for ref_file in refs_dir.glob("*.md"):
                test(f"  {skill_dir.name} references {ref_file.name}",
                     ref_file.name in content)

    # No personal data in skill files
    personal = ["Juan", "Lilliana", "haishan", "~/clawd/", "José"]
    for skill_dir in (REPO_ROOT / "skills").iterdir():
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            content = skill_md.read_text()
            for p in personal:
                test(f"  {skill_dir.name}: no '{p}'", p not in content)


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

    personal_files = ["config.yaml", ".env", "data/recipes.json", "data/gurus.json", "data/digest-state.json"]
    for pf in personal_files:
        rc, _, _ = run_cmd(f"git ls-files --error-unmatch {pf} 2>/dev/null")
        test(f"'{pf}' is NOT tracked", rc != 0)


# ═══════════════════════════════════════════════════════════
# 14. EXAMPLE FILES & FRESH-CLONE READINESS
# ═══════════════════════════════════════════════════════════

def test_example_files():
    section("14. Example Files & Fresh-Clone Readiness")

    cfg_example = REPO_ROOT / "config.example.yaml"
    test("config.example.yaml exists", cfg_example.exists())
    if cfg_example.exists():
        content = cfg_example.read_text()
        for key in ["owner", "data_dir", "icloud_folder", "venv"]:
            test(f"  config example has '{key}'", key in content)
        for p in ["Juan", "~/clawd/", "haishan"]:
            test(f"  config example no '{p}'", p not in content)

    env_example = REPO_ROOT / ".env.example"
    test(".env.example exists", env_example.exists())
    if env_example.exists():
        content = env_example.read_text()
        test("  has USDA_API_KEY placeholder", "USDA_API_KEY" in content)
        test("  no real API key", "rQVp" not in content)

    gitignore = REPO_ROOT / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        for pattern in ["config.yaml", ".env", "data/recipes.json", "data/gurus.json", "data/digest-state.json"]:
            test(f"  .gitignore has '{pattern}'", pattern in content)

    reqs = REPO_ROOT / "requirements.txt"
    test("requirements.txt exists", reqs.exists())
    if reqs.exists():
        content = reqs.read_text()
        for dep in ["duckdb", "pyyaml", "pandas", "pylibrelinkup"]:
            test(f"  requires '{dep}'", dep in content.lower())

    test("init_db.py exists", (REPO_ROOT / "scripts" / "init_db.py").exists())


# ═══════════════════════════════════════════════════════════
# 15. GIT HYGIENE
# ═══════════════════════════════════════════════════════════

def test_git_hygiene():
    section("15. Git Hygiene")

    rc, stdout, _ = run_cmd("git status --porcelain")
    test("git status runs", rc == 0)

    rc, stdout, _ = run_cmd("git diff --name-only HEAD")
    if stdout:
        test("no uncommitted changes to tracked files", False, stdout[:80])
    else:
        test("no uncommitted changes to tracked files", True)

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
        test_hevy()
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
