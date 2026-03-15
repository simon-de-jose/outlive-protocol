#!/usr/bin/env python3
"""Initialize the nutrition_log table in the health database."""

import duckdb
from pathlib import Path
from config import get_db_path

DB_PATH = get_db_path()

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
