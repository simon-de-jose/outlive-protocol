"""
USDA Food Cache — log-nutrition skill
Wraps the USDA FoodData Central API with a local DuckDB cache.
Cache lives at: $HEALTH_DATA_DIR/usda_cache.duckdb (via bootstrap.env)
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import duckdb

# Resolve bootstrap.env from repo root (skills/log-nutrition/scripts/ → repo root)
_BOOTSTRAP_ENV = Path(__file__).resolve().parent.parent.parent / "bootstrap" / "env.py"
sys.path.insert(0, str(Path(_BOOTSTRAP_ENV).parent))
from bootstrap.env import data_dir

# ── Config ────────────────────────────────────────────────────────────────────
# USDA_API_KEY comes from the environment (repo-root .env, loaded by bootstrap.env).
USDA_BASE_URL = "https://api.nal.usda.gov/fdc/v1"

NUTRIENT_ID_TO_FIELD = {
    1008: "calories",
    1003: "protein_g",
    1004: "fat_g",
    1005: "carbs_g",
    1079: "fiber_g",
    1093: "sodium_mg",
    1253: "cholesterol_mg",
}

CACHE_DB_PATH = data_dir() / "usda_cache.duckdb"

# ── DB setup ──────────────────────────────────────────────────────────────────

def _get_conn():
    """Return a duckdb connection to the cache DB, creating schema if needed."""
    CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(CACHE_DB_PATH))
    con.execute("""
        CREATE TABLE IF NOT EXISTS usda_food_cache (
            fdc_id          BIGINT PRIMARY KEY,
            description     TEXT,
            protein_g       FLOAT,
            carbs_g         FLOAT,
            fat_g           FLOAT,
            fiber_g         FLOAT,
            sugar_g         FLOAT,
            sodium_mg       FLOAT,
            cholesterol_mg  FLOAT,
            calories        FLOAT,
            cached_at       TIMESTAMP DEFAULT NOW()
        )
    """)
    return con


# ── Public API ────────────────────────────────────────────────────────────────

def get_cached(fdc_id: int) -> Optional[dict]:
    """Return cached nutrient dict for fdc_id, or None if not in cache."""
    con = _get_conn()
    row = con.execute(
        "SELECT description, protein_g, carbs_g, fat_g, fiber_g, sugar_g, "
        "sodium_mg, cholesterol_mg, calories FROM usda_food_cache WHERE fdc_id = ?",
        [int(fdc_id)]
    ).fetchone()
    con.close()
    if row is None:
        return None
    keys = ["description", "protein_g", "carbs_g", "fat_g", "fiber_g",
            "sugar_g", "sodium_mg", "cholesterol_mg", "calories"]
    return dict(zip(keys, row))


def cache_food(fdc_id: int, nutrient_dict: dict) -> None:
    """Insert or update a food entry in the cache."""
    con = _get_conn()
    con.execute("""
        INSERT INTO usda_food_cache
            (fdc_id, description, protein_g, carbs_g, fat_g, fiber_g, sugar_g,
             sodium_mg, cholesterol_mg, calories, cached_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW())
        ON CONFLICT (fdc_id) DO UPDATE SET
            description     = EXCLUDED.description,
            protein_g       = EXCLUDED.protein_g,
            carbs_g         = EXCLUDED.carbs_g,
            fat_g           = EXCLUDED.fat_g,
            fiber_g         = EXCLUDED.fiber_g,
            sugar_g         = EXCLUDED.sugar_g,
            sodium_mg       = EXCLUDED.sodium_mg,
            cholesterol_mg  = EXCLUDED.cholesterol_mg,
            calories        = EXCLUDED.calories,
            cached_at       = NOW()
    """, [
        int(fdc_id),
        nutrient_dict.get("description"),
        nutrient_dict.get("protein_g"),
        nutrient_dict.get("carbs_g"),
        nutrient_dict.get("fat_g"),
        nutrient_dict.get("fiber_g"),
        nutrient_dict.get("sugar_g"),
        nutrient_dict.get("sodium_mg"),
        nutrient_dict.get("cholesterol_mg"),
        nutrient_dict.get("calories"),
    ])
    con.close()


def _fetch_from_usda(fdc_id: int) -> Optional[dict]:
    """Fetch a single food item from USDA FoodData Central API via curl."""
    api_key = os.environ.get("USDA_API_KEY", "")
    if not api_key:
        print("USDA_API_KEY not set. Add it to .env in the repo root.", file=sys.stderr)
        return None
    url = f"{USDA_BASE_URL}/food/{fdc_id}?api_key={api_key}"
    result = subprocess.run(
        ["curl", "-s", "--max-time", "15", url],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    if "error" in data or "foodNutrients" not in data:
        return None

    nutrients = {}
    # Parse nutrients array: each entry has nutrient.id and amount
    for entry in data.get("foodNutrients", []):
        nutrient = entry.get("nutrient", {})
        nid = nutrient.get("id")
        amount = entry.get("amount")
        if nid in NUTRIENT_ID_TO_FIELD and amount is not None:
            field = NUTRIENT_ID_TO_FIELD[nid]
            nutrients[field] = float(amount)

    # Also try to grab sugar separately (nutrient 2000 = total sugars in some datasets)
    for entry in data.get("foodNutrients", []):
        nutrient = entry.get("nutrient", {})
        nid = nutrient.get("id")
        amount = entry.get("amount")
        if nid == 2000 and amount is not None:
            nutrients["sugar_g"] = float(amount)

    nutrients["description"] = data.get("description", "")
    return nutrients


def get_or_fetch(fdc_id: int) -> Optional[dict]:
    """Check cache first; on miss fetch from USDA, store, and return."""
    cached = get_cached(fdc_id)
    if cached is not None:
        return cached

    fetched = _fetch_from_usda(fdc_id)
    if fetched is not None:
        cache_food(fdc_id, fetched)
    return fetched


def batch_get_or_fetch(fdc_ids: list) -> dict:
    """
    Fetch nutrients for multiple FDC IDs.
    Returns dict mapping fdc_id → nutrient dict (or None on failure).
    Cache hits are instant; misses are fetched from USDA sequentially.
    """
    results = {}
    misses = []

    # Check cache for all IDs first
    con = _get_conn()
    for fdc_id in fdc_ids:
        row = con.execute(
            "SELECT description, protein_g, carbs_g, fat_g, fiber_g, sugar_g, "
            "sodium_mg, cholesterol_mg, calories FROM usda_food_cache WHERE fdc_id = ?",
            [int(fdc_id)]
        ).fetchone()
        if row is not None:
            keys = ["description", "protein_g", "carbs_g", "fat_g", "fiber_g",
                    "sugar_g", "sodium_mg", "cholesterol_mg", "calories"]
            results[fdc_id] = dict(zip(keys, row))
        else:
            misses.append(fdc_id)
    con.close()

    # Fetch misses from USDA
    for fdc_id in misses:
        fetched = _fetch_from_usda(fdc_id)
        if fetched is not None:
            cache_food(fdc_id, fetched)
            results[fdc_id] = fetched
        else:
            results[fdc_id] = None

    return results


# ── CLI helper for pre-seeding ────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        # Seed from a list of FDC IDs passed as space-separated args
        ids = [int(x) for x in sys.argv[2:]]
        print(f"Seeding {len(ids)} IDs...")
        for i, fdc_id in enumerate(ids):
            result = get_or_fetch(fdc_id)
            status = "OK" if result else "FAIL"
            print(f"  [{i+1}/{len(ids)}] {fdc_id}: {status} — {result.get('description', '') if result else ''}")
        print("Done.")
