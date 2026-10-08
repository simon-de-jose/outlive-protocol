#!/usr/bin/env python3
"""
Dry-run inventory-aware meal logging without touching production data.

Creates a temporary DuckDB, simulates inventory matching + subtraction against a
copy of inventory.json, inserts a synthetic nutrition_log row into the temp DB,
and prints the resulting inventory delta and logged row.

Usage:
    cd <repo>
    python skills/log-nutrition/evals/dry_run_meal.py --example stir_fry
    python skills/log-nutrition/evals/dry_run_meal.py --json '{...}'
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
EVAL_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = EVAL_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from inventory import match_ingredient, parse_confirmation, subtract_inventory
from nutrition_ingest import ingest_nutrition, migrate_database

INVENTORY_PATH = Path.home() / "clawd" / "skills" / "grocery" / "inventory.json"


EXAMPLES = {
    "stir_fry": {
        "meal": {
            "meal_time": "2026-04-12T18:30:00",
            "meal_type": "dinner",
            "meal_name": "Ground chicken tofu stir fry",
            "meal_description": "Dry-run example with inventory-aware matching",
            "food_items": [
                {"item": "ground chicken", "portion": "1 pack", "calories": 320, "protein_g": 38.0, "carbs_g": 0.0, "fat_g": 18.0},
                {"item": "tofu", "portion": "1 block", "calories": 160, "protein_g": 16.0, "carbs_g": 6.0, "fat_g": 9.0},
                {"item": "brown onion", "portion": "0.5 lbs", "calories": 90, "protein_g": 2.0, "carbs_g": 21.0, "fat_g": 0.0},
            ],
            "calories": 570,
            "protein_g": 56.0,
            "carbs_g": 27.0,
            "fat_total_g": 27.0,
            "fat_saturated_g": 6.0,
            "fat_unsaturated_g": 18.0,
            "fat_trans_g": 0.0,
            "fiber_g": 4.0,
            "sugar_g": 7.0,
            "sodium_mg": 520.0,
            "cholesterol_mg": 110.0,
            "source": "dry-run",
            "notes": "Temporary dry-run entry only",
        },
        "inventory_confirmations": {
            "ground chicken": "1 pack",
            "tofu": "1 block",
            "brown onion": "0.5 lbs",
        },
    }
}


def load_inventory() -> dict:
    if not INVENTORY_PATH.exists():
        raise FileNotFoundError(f"inventory.json not found at {INVENTORY_PATH}")
    with open(INVENTORY_PATH) as f:
        return json.load(f)


def create_temp_db(db_path: Path) -> None:
    migrate_database(db_path)


def insert_temp_meal(db_path: Path, meal: dict) -> tuple[int, dict]:
    payload = deepcopy(meal)
    entry_id = ingest_nutrition(db_path, payload)["result"]["entry"]["entry_id"]
    con = duckdb.connect(str(db_path), read_only=True)
    row = con.execute(
        "SELECT entry_id, meal_name, calories, protein_g, carbs_g, fat_total_g, source FROM nutrition_log WHERE entry_id = ?",
        [entry_id],
    ).fetchone()
    con.close()
    return entry_id, {
        "entry_id": row[0],
        "meal_name": row[1],
        "calories": row[2],
        "protein_g": row[3],
        "carbs_g": row[4],
        "fat_total_g": row[5],
        "source": row[6],
    }


def simulate_inventory(items: dict[str, dict], confirmations: dict[str, str]) -> tuple[list[dict], list, dict[str, dict]]:
    working = deepcopy(items)
    matches = []
    consumptions: list[tuple[str, float]] = []

    for mention, confirmation in confirmations.items():
        mr = match_ingredient(mention, working)
        if not mr.matched or mr.item is None:
            matches.append({
                "mention": mention,
                "matched": False,
                "confirmation": confirmation,
            })
            continue

        amount = parse_confirmation(confirmation, mr.item)
        matches.append({
            "mention": mention,
            "matched": True,
            "inventory_key": mr.item.key,
            "inventory_name": mr.item.name,
            "on_hand": mr.item.quantity,
            "unit": mr.item.unit,
            "suggestion": mr.suggestion,
            "confirmation": confirmation,
            "parsed_amount": amount,
        })
        if amount is not None:
            consumptions.append((mr.item.key, amount))

    consumed = subtract_inventory(working, consumptions)
    return matches, consumed, working


def build_report(input_payload: dict) -> dict:
    inventory = load_inventory()
    items = inventory.get("items", {})
    matches, consumed, resulting_items = simulate_inventory(items, input_payload["inventory_confirmations"])

    tmpdir = Path(tempfile.mkdtemp(prefix="nutrition-dry-run-"))
    db_path = tmpdir / "dry_run.duckdb"
    create_temp_db(db_path)
    entry_id, inserted = insert_temp_meal(db_path, input_payload["meal"])

    return {
        "temp_db": str(db_path),
        "inventory_source": str(INVENTORY_PATH),
        "meal_logged": inserted,
        "matches": matches,
        "consumed": [
            {
                "key": c.key,
                "name": c.name,
                "amount": c.amount,
                "unit": c.unit,
                "remaining": c.remaining,
                "depleted": c.depleted,
            }
            for c in consumed
        ],
        "resulting_inventory_preview": {
            c.key: {
                "name": resulting_items[c.key]["name"],
                "quantity": resulting_items[c.key]["quantity"],
                "unit": resulting_items[c.key]["unit"],
            }
            for c in consumed
        },
        "writes": {
            "production_db_touched": False,
            "production_inventory_touched": False,
            "temp_entry_id": entry_id,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Dry-run inventory-aware meal logging")
    parser.add_argument("--example", choices=sorted(EXAMPLES.keys()), help="Run a built-in example")
    parser.add_argument("--json", help="JSON payload with keys: meal, inventory_confirmations")
    args = parser.parse_args()

    if bool(args.example) == bool(args.json):
        parser.error("Choose exactly one of --example or --json")

    if args.example:
        payload = EXAMPLES[args.example]
    else:
        payload = json.loads(args.json)

    report = build_report(payload)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
