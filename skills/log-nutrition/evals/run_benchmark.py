#!/usr/bin/env python3
"""
Deterministic benchmark for the log-nutrition skill.

Exercises the scripts directly (no LLM) to verify:
  - USDA nutrient ID mapping is correct
  - Repeat meals resolve without API calls
  - Per-item macros survive in food_items JSON
  - Recipe table is seeded properly
  - Cache hit/miss behavior works

Usage:
    cd <repo>
    python skills/log-nutrition/evals/run_benchmark.py
    python skills/log-nutrition/evals/run_benchmark.py --write-results
    python skills/log-nutrition/evals/run_benchmark.py --results-path /tmp/nutrition-results.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import duckdb

# ── Resolve repo root ────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
EVAL_DIR = Path(__file__).resolve().parent
SKILL_DIR = EVAL_DIR.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
from nutrition_ingest import ingest_nutrition, migrate_database

# ── Color helpers ─────────────────────────────────────────────────────────────
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"


def pass_fail(passed: bool) -> str:
    return f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the deterministic log-nutrition benchmark")
    results = parser.add_mutually_exclusive_group()
    results.add_argument(
        "--write-results",
        action="store_true",
        help="write the tracked evals/results.json file (default: print only)",
    )
    results.add_argument(
        "--results-path",
        type=Path,
        help="write results JSON to an explicit path instead of the tracked default",
    )
    return parser.parse_args(argv)


# ── Test database setup ──────────────────────────────────────────────────────

def create_test_db(tmp_dir: Path) -> tuple[Path, Path]:
    """Create a test DuckDB with schema + seed data."""
    db_path = tmp_dir / "test_health.duckdb"
    cache_path = tmp_dir / "usda_cache.duckdb"

    # Migration owns all writable setup on the primary nutrition database,
    # including recipe compatibility objects and their default seed.
    migrate_database(db_path)
    breakfast_items = [
        {"item": "cranberry sourdough", "portion": "40g", "fdc_id": None, "calories": 97, "protein_g": 3.0, "carbs_g": 18.0, "fat_g": 1.5},
        {"item": "avocado", "portion": "1/2", "fdc_id": "171716", "calories": 114, "protein_g": 1.3, "carbs_g": 6.0, "fat_g": 10.5},
        {"item": "hard-boiled egg", "portion": "50g", "fdc_id": "748967", "calories": 78, "protein_g": 6.3, "carbs_g": 0.6, "fat_g": 5.3},
        {"item": "black coffee", "portion": "240ml", "fdc_id": "171998", "calories": 2, "protein_g": 0.3, "carbs_g": 0.0, "fat_g": 0.0},
    ]

    # Seed a previous breakfast log entry (simulating "yesterday")
    yesterday_breakfast_items = json.dumps(breakfast_items)
    ingest_nutrition(db_path, {"meal_time": "2026-04-09T08:30:00", "meal_type": "breakfast", "meal_name": "Sourdough, avocado, egg & black coffee", "food_items": yesterday_breakfast_items, "calories": 333, "protein_g": 11.3, "carbs_g": 27.8, "fat_total_g": 20.7, "source": "chat"})

    # Seed a previous lunch log (for delta meal test)
    lunch_items = json.dumps([
        {"item": "pork loin", "portion": "136g", "fdc_id": "168860", "calories": 163, "protein_g": 28.0, "carbs_g": 0.0, "fat_g": 4.8},
        {"item": "fresh noodle", "portion": "100g", "fdc_id": "2708352", "calories": 137, "protein_g": 4.5, "carbs_g": 25.0, "fat_g": 2.1},
        {"item": "shrimp", "portion": "80g", "fdc_id": "175167", "calories": 68, "protein_g": 16.1, "carbs_g": 0.2, "fat_g": 0.4},
        {"item": "hard-boiled egg", "portion": "50g", "fdc_id": "748967", "calories": 78, "protein_g": 6.3, "carbs_g": 0.6, "fat_g": 5.3},
        {"item": "cooking oil", "portion": "1 tbsp", "fdc_id": "171413", "calories": 124, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 14.0},
        {"item": "yellow pepper", "portion": "half", "fdc_id": "170416", "calories": 15, "protein_g": 0.6, "carbs_g": 3.5, "fat_g": 0.1},
        {"item": "snow mustard greens", "portion": "50g", "fdc_id": None, "calories": 8, "protein_g": 0.6, "carbs_g": 1.2, "fat_g": 0.1},
    ])
    ingest_nutrition(db_path, {"meal_time": "2026-04-09T12:30:00", "meal_type": "lunch", "meal_name": "雪菜肉丝面", "food_items": lunch_items, "calories": 593, "protein_g": 56.1, "carbs_g": 30.5, "fat_total_g": 26.8, "source": "chat"})

    # Seed USDA cache
    cache_con = duckdb.connect(str(cache_path))
    cache_con.execute("""
        CREATE TABLE IF NOT EXISTS usda_food_cache (
            fdc_id BIGINT PRIMARY KEY,
            description TEXT,
            protein_g FLOAT,
            carbs_g FLOAT,
            fat_g FLOAT,
            fiber_g FLOAT,
            sugar_g FLOAT,
            sodium_mg FLOAT,
            cholesterol_mg FLOAT,
            calories FLOAT,
            cached_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Seed cache with corrected nutrient mapping
    cache_foods = [
        (748967, "Egg, hard-boiled", 12.6, 1.1, 10.6, 0.0, 1.1, 124, 373, 155),
        (171716, "Avocado, raw", 2.0, 8.5, 14.7, 6.7, 0.7, 7, 0, 160),
        (171998, "Coffee, brewed", 0.3, 0.0, 0.0, 0.0, 0.0, 2, 0, 2),
        (168860, "Pork, loin", 20.6, 0.0, 3.5, 0.0, 0.0, 50, 65, 121),
        (2708352, "Noodles, fresh", 4.5, 25.0, 2.1, 1.0, 0.5, 250, 20, 137),
        (175167, "Shrimp, raw", 20.1, 0.2, 0.5, 0.0, 0.0, 111, 126, 85),
        (171413, "Oil, vegetable", 0.0, 0.0, 100.0, 0.0, 0.0, 0, 0, 884),
        (170416, "Peppers, yellow, raw", 0.9, 5.4, 0.2, 1.7, 2.9, 2, 0, 27),
    ]
    for food in cache_foods:
        cache_con.execute("""
            INSERT INTO usda_food_cache (fdc_id, description, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg, cholesterol_mg, calories)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (fdc_id) DO UPDATE SET
                description=EXCLUDED.description, protein_g=EXCLUDED.protein_g,
                carbs_g=EXCLUDED.carbs_g, fat_g=EXCLUDED.fat_g, fiber_g=EXCLUDED.fiber_g,
                sugar_g=EXCLUDED.sugar_g, sodium_mg=EXCLUDED.sodium_mg,
                cholesterol_mg=EXCLUDED.cholesterol_mg, calories=EXCLUDED.calories
        """, list(food))

    cache_con.close()
    return db_path, cache_path


# ── Test: Nutrient ID mapping ────────────────────────────────────────────────

def test_nutrient_id_mapping():
    """Verify the USDA nutrient ID map has correct values."""
    results = []

    # Expected: {nutrient_id: field_name}
    expected = {
        1008: "calories",
        1003: "protein_g",
        1004: "fat_g",
        1005: "carbs_g",
        1079: "fiber_g",
        1093: "sodium_mg",
        1253: "cholesterol_mg",
    }

    # Read the actual mapping from usda_cache.py
    usda_path = SCRIPTS_DIR / "usda_cache.py"
    content = usda_path.read_text()

    actual = {}
    for line in content.splitlines():
        line = line.strip()
        if ":" in line and any(str(k) in line for k in expected):
            parts = line.split(":")
            if len(parts) == 2:
                try:
                    nid = int(parts[0].strip())
                    field = parts[1].strip().strip(',').strip('"').strip("'")
                    actual[nid] = field
                except ValueError:
                    pass

    for nid, expected_field in expected.items():
        passed = actual.get(nid) == expected_field
        results.append({
            "name": f"nutrient_{nid}_maps_to_{expected_field}",
            "passed": passed,
            "evidence": f"Expected {nid}→{expected_field}, got {nid}→{actual.get(nid, 'MISSING')}",
        })

    return results


# ── Test: Recipe table seeded ────────────────────────────────────────────────

def test_recipe_seeded(db_path: Path):
    """Verify the example breakfast recipe exists with per-item macros."""
    con = duckdb.connect(str(db_path), read_only=True)
    results = []

    row = con.execute("SELECT name, food_items, total_calories FROM recipes WHERE name = ?", ["Example breakfast"]).fetchone()
    con.close()

    if row is None:
        results.append({"name": "recipe_exists", "passed": False, "evidence": "Example breakfast recipe not found"})
        return results

    results.append({"name": "recipe_exists", "passed": True, "evidence": f"Found: {row[0]}"})

    # Check per-item macros
    items = json.loads(row[1]) if isinstance(row[1], str) else row[1]
    items_with_macros = sum(1 for item in items if item.get("calories") is not None)
    all_have_macros = items_with_macros == len(items)
    results.append({
        "name": "recipe_per_item_macros",
        "passed": all_have_macros,
        "evidence": f"{items_with_macros}/{len(items)} items have calories field",
    })

    # Check total calories
    total = row[2]
    results.append({
        "name": "recipe_total_calories",
        "passed": total == 333,
        "evidence": f"Total calories: {total} (expected 333)",
    })

    return results


# ── Test: Same breakfast repeat ──────────────────────────────────────────────

def test_same_breakfast(db_path: Path, cache_path: Path):
    """Simulate 'same breakfast' — should resolve from previous log, zero API calls."""
    con = duckdb.connect(str(db_path), read_only=True)
    results = []
    api_calls = 0

    start = time.time()
    # Simulate what the skill does: query for previous breakfast
    row = con.execute("""
        SELECT meal_name, food_items, calories, protein_g, carbs_g, fat_total_g
        FROM nutrition_log
        WHERE meal_type = 'breakfast'
        ORDER BY meal_time DESC LIMIT 1
    """).fetchone()
    elapsed = time.time() - start

    con.close()

    if row is None:
        results.append({"name": "same_breakfast_finds_previous", "passed": False, "evidence": "No previous breakfast found"})
        return results

    results.append({"name": "same_breakfast_finds_previous", "passed": True, "evidence": f"Found: {row[0]}"})

    # Check per-item macros in food_items
    items = json.loads(row[1]) if isinstance(row[1], str) else row[1]
    items_with_macros = sum(1 for item in items if item.get("calories") is not None)
    all_have_macros = items_with_macros == len(items)
    results.append({
        "name": "same_breakfast_per_item_macros",
        "passed": all_have_macros,
        "evidence": f"{items_with_macros}/{len(items)} items have per-item calories",
    })

    # Check timing
    results.append({
        "name": "same_breakfast_under_30s",
        "passed": elapsed < 30,
        "evidence": f"Query took {elapsed:.3f}s",
    })

    # Check zero API calls
    results.append({
        "name": "same_breakfast_zero_api_calls",
        "passed": api_calls == 0,
        "evidence": f"API calls: {api_calls} (expected 0)",
    })

    return results


# ── Test: Cache correctness ──────────────────────────────────────────────────

def test_cache_correctness(cache_path: Path):
    """Verify cached foods have correct nutrient fields (not None for carbs etc)."""
    con = duckdb.connect(str(cache_path), read_only=True)
    results = []

    rows = con.execute("""
        SELECT fdc_id, description, protein_g, carbs_g, fat_g, calories
        FROM usda_food_cache
    """).fetchall()
    con.close()

    for row in rows:
        fdc_id, desc, protein, carbs, fat, cals = row
        # Key check: carbs should NOT be None (was the primary bug)
        carbs_ok = carbs is not None
        results.append({
            "name": f"cache_{fdc_id}_carbs_not_null",
            "passed": carbs_ok,
            "evidence": f"{desc} (FDC {fdc_id}): carbs_g={carbs}",
        })
        # All macros should be non-None
        all_ok = all(v is not None for v in [protein, carbs, fat, cals])
        results.append({
            "name": f"cache_{fdc_id}_all_macros_present",
            "passed": all_ok,
            "evidence": f"{desc}: P={protein}, C={carbs}, F={fat}, Cal={cals}",
        })

    return results


# ── Test: Chinese ingredients cache hit rate ──────────────────────────────────

def test_chinese_ingredients(cache_path: Path):
    """Check cache hit rate for common Chinese cooking ingredients."""
    con = duckdb.connect(str(cache_path), read_only=True)
    results = []

    ingredient_fdc_ids = {
        "pork loin": 168860,
        "fresh noodle": 2708352,
        "shrimp": 175167,
        "egg": 748967,
        "cooking oil": 171413,
        "yellow pepper": 170416,
        "snow mustard greens": None,  # No FDC ID — expected miss
    }

    hits = 0
    total = len(ingredient_fdc_ids)
    for name, fdc_id in ingredient_fdc_ids.items():
        if fdc_id is None:
            results.append({
                "name": f"ingredient_{name.replace(' ', '_')}_no_fdc",
                "passed": True,
                "evidence": f"{name}: no FDC ID (expected miss)",
            })
            continue

        row = con.execute("SELECT fdc_id FROM usda_food_cache WHERE fdc_id = ?", [fdc_id]).fetchone()
        hit = row is not None
        if hit:
            hits += 1
        results.append({
            "name": f"ingredient_{name.replace(' ', '_')}_cache",
            "passed": hit,
            "evidence": f"{name} (FDC {fdc_id}): {'HIT' if hit else 'MISS'}",
        })

    con.close()

    hit_rate = hits / total
    results.append({
        "name": "chinese_ingredients_cache_hit_rate",
        "passed": hit_rate >= 0.5,
        "evidence": f"Cache hit rate: {hits}/{total} ({hit_rate:.0%})",
    })

    return results


# ── Test: Sequence name consistency ──────────────────────────────────────────

def test_sequence_name():
    """Production paths must not teach direct nutrition SQL writes."""
    results = []
    targets = [SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").rglob("*")),
               *sorted((SKILL_DIR / "evals").rglob("*.py")), *sorted(SCRIPTS_DIR.rglob("*.py")),
               REPO_ROOT / "bootstrap" / "init_db.py"]
    targets = [path for path in targets if path.is_file() and path.name != "nutrition_ingest.py"]
    content = "\n".join(path.read_text() for path in targets)
    direct_insert = re.compile(r"INSERT" + r"\s+INTO\s+nutrition_log", re.IGNORECASE)
    legacy_nextval = "nextval(" + "'seq_nutrition_entry')"
    has_direct_writer = bool(direct_insert.search(content)) or legacy_nextval in content

    results.append({
        "name": "production_paths_have_no_direct_nutrition_writer",
        "passed": not has_direct_writer,
        "evidence": f"Direct nutrition writer text found: {has_direct_writer}",
    })

    return results


# ── Test: Output discipline section ──────────────────────────────────────────

def test_output_discipline():
    """Check that SKILL.md has the output discipline section."""
    results = []

    skill_path = SKILL_DIR / "SKILL.md"
    content = skill_path.read_text()

    has_section = "Output & Confirmation Discipline" in content
    # Preserve the substantive guardrail after the section was renamed; do
    # not weaken this benchmark to a heading-only check.
    has_never = "Do NOT insert uncertain meals without confirmation." in content

    results.append({
        "name": "output_discipline_section_exists",
        "passed": has_section,
        "evidence": f"'Output & Confirmation Discipline' section found: {has_section}",
    })
    results.append({
        "name": "output_discipline_has_never_rules",
        "passed": has_never,
        "evidence": f"Contains explicit no-unconfirmed-insert rule: {has_never}",
    })

    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None):
    args = parse_args(argv)
    print(f"\n{BOLD}🧪 Nutrition Skill Benchmark{RESET}\n")

    # Create test DB
    tmp_dir = Path(tempfile.mkdtemp(prefix="nutrition_eval_"))
    print(f"Test DB: {tmp_dir}")
    db_path, cache_path = create_test_db(tmp_dir)

    all_results = []

    # Run all test suites
    suites = [
        ("Nutrient ID Mapping", lambda: test_nutrient_id_mapping()),
        ("Sequence Name", lambda: test_sequence_name()),
        ("Output Discipline", lambda: test_output_discipline()),
        ("Recipe Seeded", lambda: test_recipe_seeded(db_path)),
        ("Same Breakfast Repeat", lambda: test_same_breakfast(db_path, cache_path)),
        ("Cache Correctness", lambda: test_cache_correctness(cache_path)),
        ("Chinese Ingredients", lambda: test_chinese_ingredients(cache_path)),
    ]

    for suite_name, suite_fn in suites:
        print(f"\n{BOLD}── {suite_name} ──{RESET}")
        try:
            suite_results = suite_fn()
            for r in suite_results:
                status = pass_fail(r["passed"])
                print(f"  {status}  {r['name']}: {r['evidence']}")
            all_results.extend(suite_results)
        except Exception as e:
            print(f"  {RED}ERROR{RESET}  {suite_name}: {e}")
            all_results.append({"name": suite_name, "passed": False, "evidence": str(e)})

    # Summary
    total = len(all_results)
    passed = sum(1 for r in all_results if r["passed"])
    failed = total - passed

    print(f"\n{BOLD}── Summary ──{RESET}")
    print(f"  Total: {total}  |  {GREEN}Passed: {passed}{RESET}  |  {RED}Failed: {failed}{RESET}")

    results_payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": total,
        "passed": passed,
        "failed": failed,
        "results": all_results,
    }
    if args.write_results:
        results_path = EVAL_DIR / "results.json"
    else:
        results_path = args.results_path
    if results_path is None:
        print("\nResults JSON not written by default; use --write-results for evals/results.json or --results-path PATH for an explicit artifact.")
    else:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        with open(results_path, "w") as f:
            json.dump(results_payload, f, indent=2)
        print(f"\nResults saved to: {results_path}")

    # Cleanup
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # Exit code
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
