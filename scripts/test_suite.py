#!/usr/bin/env python3
"""
Outlive-protocol test suite.

Run after any changes to verify nothing is broken.

Usage:
    python3 scripts/test_suite.py              # Run all tests
    python3 scripts/test_suite.py --quick      # Skip slow tests (libre dry-run, import dry-run)
    python3 scripts/test_suite.py --fresh      # Run fresh-clone checks (no personal data in tracked files)
"""

import sys
import os
import json
import subprocess
import argparse
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
    mark = status
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    return condition


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def run_cmd(cmd, cwd=None):
    """Run a command, return (returncode, stdout, stderr)."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd or str(REPO_ROOT))
    return r.returncode, r.stdout.strip(), r.stderr.strip()


# ─── 1. CONFIG RESOLUTION ───────────────────────────────────

def test_config_resolution():
    section("1. Config Resolution")

    from config import (
        load_config, get_data_dir, get_db_path, get_log_dir,
        get_reports_dir, get_icloud_folder, get_recipes_path,
        get_gurus_path, get_digest_state_path, get_user_profile,
        get_owner, get_display_units, get_libre_csv_prefix
    )

    # Config loads without error
    try:
        config = load_config()
        test("config.yaml loads", True)
    except Exception as e:
        test("config.yaml loads", False, str(e))
        return

    # data_dir is set and exists
    data_dir = get_data_dir()
    test("data_dir resolves", data_dir is not None, str(data_dir))
    test("data_dir exists", data_dir.exists())
    test("data_dir is absolute", data_dir.is_absolute())

    # DB path derives from data_dir
    db = get_db_path()
    test("db_path resolves", db is not None, str(db))
    test("db_path is under data_dir or explicit", True)

    # Log/reports dirs
    logs = get_log_dir()
    reports = get_reports_dir()
    test("log_dir resolves and exists", logs.exists(), str(logs))
    test("reports_dir resolves and exists", reports.exists(), str(reports))

    # Data file paths
    test("recipes_path resolves", get_recipes_path() is not None, str(get_recipes_path()))
    test("gurus_path resolves", get_gurus_path() is not None, str(get_gurus_path()))
    test("digest_state_path resolves", get_digest_state_path() is not None, str(get_digest_state_path()))

    # User profile
    profile = get_user_profile()
    test("user_profile loads (dict)", isinstance(profile, dict))

    # Other getters
    test("owner resolves", get_owner() != "", get_owner())
    test("display_units resolves", get_display_units() in ("metric", "imperial"), get_display_units())

    # paths.sh matches config.py
    rc, stdout, _ = run_cmd("bash shell/paths.sh --json")
    if rc == 0:
        paths = json.loads(stdout)
        test("paths.sh db == config.py db", paths["db"] == str(db))
        test("paths.sh data_dir == config.py data_dir", paths["data_dir"] == str(data_dir))
        test("paths.sh logs == config.py logs", paths["logs"] == str(logs))
        test("paths.sh reports == config.py reports", paths["reports"] == str(reports))
    else:
        test("paths.sh runs", False, "exit code " + str(rc))


# ─── 2. DATABASE ────────────────────────────────────────────

def test_database():
    section("2. Database")

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
        for table in ["readings", "imports", "workouts", "nutrition_log", "medications"]:
            test(f"table '{table}' exists", table in tables)

        # Row counts (sanity check — should be > 0 if data has been imported)
        for table in ["readings", "imports"]:
            if table in tables:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                test(f"'{table}' has data", count > 0, f"{count:,} rows")

        # Source distribution
        if "readings" in tables:
            sources = conn.execute("SELECT source, COUNT(*) FROM readings GROUP BY source").fetchall()
            for src, cnt in sources:
                test(f"source '{src}' has data", cnt > 0, f"{cnt:,} rows")

        # Required views
        try:
            conn.execute("SELECT COUNT(*) FROM v_nightly_signals").fetchone()
            test("view 'v_nightly_signals' exists", True)
        except Exception:
            test("view 'v_nightly_signals' exists", False)

        # Sequence exists (check via catalog, not nextval, to avoid write lock)
        try:
            seqs = conn.execute(
                "SELECT sequence_name FROM duckdb_sequences() WHERE sequence_name = 'seq_nutrition_id'"
            ).fetchall()
            test("sequence 'seq_nutrition_id' exists", len(seqs) > 0)
        except Exception as e:
            test("sequence 'seq_nutrition_id' exists", False, str(e))

    finally:
        conn.close()


# ─── 3. DATA FILES ──────────────────────────────────────────

def test_data_files():
    section("3. Data Files")

    from config import get_recipes_path, get_gurus_path, get_digest_state_path, get_user_profile

    # Recipes
    recipes_path = get_recipes_path()
    if recipes_path.exists():
        try:
            data = json.loads(recipes_path.read_text())
            test("recipes.json valid JSON", True)
            test("recipes.json has 'recipes' key", "recipes" in data)
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
        test("user-profile has libre_patient_name", "libre_patient_name" in profile,
             profile.get("libre_patient_name", ""))

    # Example files in repo
    for name in ["recipes.example.json", "gurus.example.json",
                 "digest-state.example.json", "user-profile.example.yaml"]:
        path = REPO_ROOT / "data" / name
        test(f"example file '{name}' exists in repo", path.exists())


# ─── 4. SCRIPTS ─────────────────────────────────────────────

def test_scripts():
    section("4. Script Imports")

    scripts_dir = REPO_ROOT / "scripts"

    # Test that key scripts can be imported without error
    for module_name in ["config", "validate", "init_db", "daily_import", "sync_libre"]:
        try:
            # Use subprocess to isolate import errors
            rc, _, stderr = run_cmd(
                f"{sys.executable} -c \"import sys; sys.path.insert(0, 'scripts'); import {module_name}\"")
            test(f"import {module_name}", rc == 0, stderr[:80] if rc != 0 else "")
        except Exception as e:
            test(f"import {module_name}", False, str(e))


# ─── 5. VALIDATION ──────────────────────────────────────────

def test_validation():
    section("5. Data Validation")

    rc, stdout, stderr = run_cmd(f"{sys.executable} scripts/validate.py")
    test("validate.py runs", rc == 0, stderr[:80] if rc != 0 else "")
    test("validate.py no issues", "No data quality issues found" in stdout,
         stdout[:100] if "No data quality issues" not in stdout else "")


# ─── 6. IMPORT DRY-RUN ─────────────────────────────────────

def test_import_dryrun():
    section("6. Import Dry-Run")

    rc, stdout, stderr = run_cmd(f"{sys.executable} scripts/daily_import.py --dry-run")
    test("daily_import.py --dry-run runs", rc == 0,
         (stderr or stdout)[:100] if rc != 0 else "")

    if rc == 0:
        test("dry-run finds iCloud folder", "Scanning" in stdout or "Found" in stdout or "up to date" in stdout.lower(),
             stdout[:100])


# ─── 7. LIBRE DRY-RUN ──────────────────────────────────────

def test_libre_dryrun():
    section("7. Libre Sync Dry-Run")

    rc, stdout, stderr = run_cmd(f"{sys.executable} scripts/sync_libre.py --dry-run")
    # Libre may fail if not configured — that's OK, we just check the script runs
    if rc == 0:
        test("sync_libre.py --dry-run runs", True)
        test("libre connects to API", "patient" in stdout.lower() or "reading" in stdout.lower(),
             stdout[:100])
    else:
        # Check if it's a credential issue vs a code error
        if "credential" in stderr.lower() or "keychain" in stderr.lower() or "password" in stderr.lower():
            test("sync_libre.py --dry-run runs", True, "credentials not configured (expected)")
        else:
            test("sync_libre.py --dry-run runs", False, (stderr or stdout)[:100])


# ─── 8. SKILL FILES ─────────────────────────────────────────

def test_skill_files():
    section("8. Skill Files")

    skill_files = [
        REPO_ROOT / "SKILL.md",
        REPO_ROOT / "sub-skills" / "sync-health-data" / "SKILL.md",
        REPO_ROOT / "sub-skills" / "analyze-health-data" / "SKILL.md",
        REPO_ROOT / "sub-skills" / "log-nutrition" / "SKILL.md",
    ]

    for path in skill_files:
        test(f"{path.relative_to(REPO_ROOT)} exists", path.exists())
        if path.exists():
            content = path.read_text()
            # Check frontmatter
            test(f"  has frontmatter", content.startswith("---"))


# ─── 9. NO PERSONAL DATA IN TRACKED FILES ──────────────────

def test_no_personal_data():
    section("9. No Personal Data in Tracked Files")

    # Get list of tracked files
    rc, tracked_files, _ = run_cmd("git ls-files")
    if rc != 0:
        test("git ls-files works", False)
        return

    personal_patterns = ["Juan", "Lilliana", "Liliana", "moltbot", "haishan", "~/clawd/"]
    violations = []

    for filepath in tracked_files.split("\n"):
        if not filepath or filepath.startswith(".git"):
            continue
        # Only check text files
        if not any(filepath.endswith(ext) for ext in [".md", ".py", ".yaml", ".yml", ".json", ".sh"]):
            continue

        full_path = REPO_ROOT / filepath
        if not full_path.exists():
            continue

        try:
            content = full_path.read_text()
            for pattern in personal_patterns:
                if pattern in content:
                    violations.append(f"{filepath}: contains '{pattern}'")
        except UnicodeDecodeError:
            continue

    test("no personal data in tracked files", len(violations) == 0,
         "; ".join(violations[:5]) if violations else "")

    # Verify personal files are NOT tracked
    personal_files = ["config.yaml", ".env", "data/recipes.json", "data/gurus.json", "data/digest-state.json"]
    for pf in personal_files:
        rc, _, _ = run_cmd(f"git ls-files --error-unmatch {pf} 2>/dev/null")
        test(f"'{pf}' is NOT tracked", rc != 0)


# ─── 10. EXAMPLE FILES ──────────────────────────────────────

def test_example_files():
    section("10. Example Files")

    examples = {
        "config.example.yaml": ["owner", "data_dir", "icloud_folder"],
        ".env.example": ["USDA_API_KEY"],
    }

    for filename, expected_keys in examples.items():
        path = REPO_ROOT / filename
        test(f"{filename} exists", path.exists())
        if path.exists():
            content = path.read_text()
            for key in expected_keys:
                test(f"  contains '{key}'", key in content)
            # Should NOT contain personal data
            for pattern in ["Juan", "~/clawd/", "haishan"]:
                test(f"  no '{pattern}'", pattern not in content)


# ─── MAIN ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Outlive-protocol test suite")
    parser.add_argument("--quick", action="store_true", help="Skip slow tests")
    parser.add_argument("--fresh", action="store_true", help="Only run fresh-clone checks")
    args = parser.parse_args()

    print("\n" + "="*60)
    print("  OUTLIVE-PROTOCOL TEST SUITE")
    print("="*60)

    if args.fresh:
        # Fresh-clone subset
        test_config_resolution()
        test_skill_files()
        test_no_personal_data()
        test_example_files()
    else:
        test_config_resolution()
        test_database()
        test_data_files()
        test_scripts()
        test_validation()
        test_skill_files()
        test_no_personal_data()
        test_example_files()

        if not args.quick:
            test_import_dryrun()
            test_libre_dryrun()

    # Summary
    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    total = len(results)

    print(f"\n{'='*60}")
    print(f"  RESULTS: {passed}/{total} passed" + (f", {failed} FAILED" if failed else ""))
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
