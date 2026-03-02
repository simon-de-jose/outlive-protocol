#!/usr/bin/env python3
"""
Log a nutrition entry to the database.

Usage:
    python log_nutrition.py --json '{...}'
    
JSON format:
{
    "meal_time": "2026-02-07T19:30:00",
    "meal_type": "dinner",
    "meal_name": "Grilled chicken with rice",
    "meal_description": "Home-cooked, olive oil, steamed broccoli on the side",
    "food_items": [
        {"item": "chicken breast", "portion": "6oz", "calories": 280},
        {"item": "white rice", "portion": "3/4 cup", "calories": 160},
        {"item": "broccoli", "portion": "1 cup", "calories": 55}
    ],
    "calories": 520,
    "protein_g": 48,
    "carbs_g": 38,
    "fat_total_g": 12,
    "fat_saturated_g": 2.5,
    "fat_unsaturated_g": 8,
    "fat_trans_g": 0,
    "fiber_g": 4,
    "sugar_g": 2,
    "sodium_mg": 380,
    "potassium_mg": 620,
    "calcium_mg": 45,
    "iron_mg": 1.8,
    "magnesium_mg": 65,
    "vitamin_d_mcg": 0.2,
    "vitamin_b12_mcg": 0.6,
    "vitamin_c_mg": 85,
    "cholesterol_mg": 140,
    "source": "chat",
    "notes": "Felt great after this meal"
}
"""

import argparse
import json
import duckdb
from pathlib import Path
from datetime import datetime
from config import get_db_path

DB_PATH = get_db_path()

NUTRITION_FIELDS = [
    "meal_time", "meal_type", "meal_name", "meal_description", "food_items",
    "calories", "protein_g", "carbs_g", 
    "fat_total_g", "fat_saturated_g", "fat_unsaturated_g", "fat_trans_g",
    "fiber_g", "sugar_g",
    "sodium_mg", "potassium_mg", "calcium_mg", "iron_mg", "magnesium_mg",
    "vitamin_d_mcg", "vitamin_b12_mcg", "vitamin_c_mg",
    "cholesterol_mg",
    "source", "notes"
]

def log_nutrition(data: dict) -> int:
    """Log a nutrition entry and return the entry_id."""
    
    conn = duckdb.connect(str(DB_PATH))
    
    # Get next entry_id
    result = conn.execute("SELECT nextval('seq_nutrition_entry')").fetchone()
    entry_id = result[0]
    
    # Convert food_items list to JSON string if needed
    if "food_items" in data and isinstance(data["food_items"], list):
        data["food_items"] = json.dumps(data["food_items"])
    
    # Build insert statement
    fields = ["entry_id"] + [f for f in NUTRITION_FIELDS if f in data]
    placeholders = ["?"] * len(fields)
    values = [entry_id] + [data.get(f) for f in fields[1:]]
    
    sql = f"""
        INSERT INTO nutrition_log ({', '.join(fields)})
        VALUES ({', '.join(placeholders)})
    """
    
    conn.execute(sql, values)
    conn.close()
    
    return entry_id

def main():
    parser = argparse.ArgumentParser(description="Log nutrition entry")
    parser.add_argument("--json", required=True, help="JSON data for the entry")
    args = parser.parse_args()
    
    data = json.loads(args.json)
    entry_id = log_nutrition(data)
    print(f"✅ Logged nutrition entry #{entry_id}")
    print(f"   Meal: {data.get('meal_name', 'unnamed')}")
    print(f"   Calories: {data.get('calories', 'N/A')}")

if __name__ == "__main__":
    main()
