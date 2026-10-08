#!/usr/bin/env python3
"""Initialize the nutrition_log table in the health database."""

import json
import duckdb
from pathlib import Path
from bootstrap.env import db_path

DB_PATH = db_path()

def init_nutrition_table():
    """Create the nutrition_log table if it doesn't exist."""
    
    conn = duckdb.connect(str(DB_PATH))
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nutrition_log (
            entry_id INTEGER PRIMARY KEY,
            
            -- When and what
            meal_time TIMESTAMP NOT NULL,
            meal_type VARCHAR,           -- breakfast, lunch, dinner, snack
            meal_name VARCHAR,           -- "Chicken stir fry", "Pain au chocolat"
            meal_description TEXT,       -- detailed description, notes about the meal
            food_items TEXT,             -- JSON array of individual food items with portions
            
            -- Macronutrients
            calories DOUBLE,
            protein_g DOUBLE,
            carbs_g DOUBLE,
            fat_total_g DOUBLE,
            fat_saturated_g DOUBLE,
            fat_unsaturated_g DOUBLE,
            fat_trans_g DOUBLE,
            
            -- Carbohydrate breakdown
            fiber_g DOUBLE,
            sugar_g DOUBLE,
            
            -- Key minerals
            sodium_mg DOUBLE,
            potassium_mg DOUBLE,
            calcium_mg DOUBLE,
            iron_mg DOUBLE,
            magnesium_mg DOUBLE,
            
            -- Key vitamins
            vitamin_d_mcg DOUBLE,
            vitamin_b12_mcg DOUBLE,
            vitamin_c_mg DOUBLE,
            
            -- Other
            cholesterol_mg DOUBLE,
            
            -- Metadata
            source VARCHAR DEFAULT 'chat',    -- chat, photo, imported
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT
        )
    """)
    
    # Create sequence for entry_id if needed
    conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS seq_nutrition_entry START 1
    """)

    conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS seq_recipe_id START 1
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY DEFAULT nextval('seq_recipe_id'),
            name VARCHAR NOT NULL,
            description VARCHAR,
            food_items JSON NOT NULL,
            total_calories DOUBLE,
            total_protein_g DOUBLE,
            total_carbs_g DOUBLE,
            total_fat_g DOUBLE,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(name)
        )
    """)

    example_breakfast_items = [
        {"item": "cranberry sourdough", "portion": "40g", "fdc_id": None, "calories": 97, "protein_g": 3.0, "carbs_g": 18.0, "fat_g": 1.5},
        {"item": "avocado", "portion": "1/2", "fdc_id": "171716", "calories": 114, "protein_g": 1.3, "carbs_g": 6.0, "fat_g": 10.5},
        {"item": "hard-boiled egg", "portion": "50g", "fdc_id": "748967", "calories": 78, "protein_g": 6.3, "carbs_g": 0.6, "fat_g": 5.3},
        {"item": "black coffee", "portion": "240ml", "fdc_id": "171998", "calories": 2, "protein_g": 0.3, "carbs_g": 0.0, "fat_g": 0.0},
    ]

    conn.execute("""
        INSERT INTO recipes (
            name, description, food_items,
            total_calories, total_protein_g, total_carbs_g, total_fat_g
        )
        SELECT ?, ?, ?::JSON, ?, ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM recipes WHERE name = ?
        )
    """, [
        "Example breakfast",
        "Cranberry sourdough, avocado, hard-boiled egg, and black coffee.",
        json.dumps(example_breakfast_items),
        333,
        11.3,
        27.8,
        20.7,
        "Example breakfast",
    ])
    
    # Create indexes for common queries
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_nutrition_meal_time 
        ON nutrition_log(meal_time)
    """)
    
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_nutrition_meal_type 
        ON nutrition_log(meal_type)
    """)
    
    conn.close()
    print("✅ nutrition_log table initialized successfully")
    print(f"   Database: {DB_PATH}")

if __name__ == "__main__":
    init_nutrition_table()
