#!/usr/bin/env python3
"""
Generate nutrition summaries from the database.

Usage:
    python nutrition_summary.py --today
    python nutrition_summary.py --date 2026-02-07
    python nutrition_summary.py --week
    python nutrition_summary.py --month
"""

import argparse
import duckdb
import json
from pathlib import Path
from datetime import datetime, timedelta
from config import get_db_path

DB_PATH = get_db_path()

def get_daily_summary(date_str: str) -> dict:
    """Get nutrition summary for a specific date."""
    
    conn = duckdb.connect(str(DB_PATH), read_only=True)
    
    # Get all meals for the day
    meals = conn.execute("""
        SELECT 
            entry_id,
            meal_time,
            meal_type,
            meal_name,
            meal_description,
            calories,
            protein_g,
            carbs_g,
            fat_total_g,
            fat_saturated_g,
            fat_unsaturated_g,
            fiber_g,
            sugar_g,
            sodium_mg
        FROM nutrition_log
        WHERE DATE(meal_time) = ?
        ORDER BY meal_time
    """, [date_str]).fetchall()
    
    # Get daily totals
    totals = conn.execute("""
        SELECT 
            COUNT(*) as meal_count,
            SUM(calories) as total_calories,
            SUM(protein_g) as total_protein,
            SUM(carbs_g) as total_carbs,
            SUM(fat_total_g) as total_fat,
            SUM(fat_saturated_g) as total_sat_fat,
            SUM(fat_unsaturated_g) as total_unsat_fat,
            SUM(fiber_g) as total_fiber,
            SUM(sugar_g) as total_sugar,
            SUM(sodium_mg) as total_sodium,
            SUM(potassium_mg) as total_potassium,
            SUM(calcium_mg) as total_calcium,
            SUM(iron_mg) as total_iron,
            SUM(magnesium_mg) as total_magnesium,
            SUM(vitamin_d_mcg) as total_vit_d,
            SUM(vitamin_b12_mcg) as total_vit_b12,
            SUM(vitamin_c_mg) as total_vit_c,
            SUM(cholesterol_mg) as total_cholesterol
        FROM nutrition_log
        WHERE DATE(meal_time) = ?
    """, [date_str]).fetchone()
    
    conn.close()
    
    return {
        "date": date_str,
        "meals": [
            {
                "entry_id": m[0],
                "time": str(m[1]),
                "type": m[2],
                "name": m[3],
                "description": m[4],
                "calories": m[5],
                "protein_g": m[6],
                "carbs_g": m[7],
                "fat_total_g": m[8]
            }
            for m in meals
        ],
        "totals": {
            "meal_count": totals[0],
            "calories": totals[1],
            "protein_g": totals[2],
            "carbs_g": totals[3],
            "fat_total_g": totals[4],
            "fat_saturated_g": totals[5],
            "fat_unsaturated_g": totals[6],
            "fiber_g": totals[7],
            "sugar_g": totals[8],
            "sodium_mg": totals[9],
            "potassium_mg": totals[10],
            "calcium_mg": totals[11],
            "iron_mg": totals[12],
            "magnesium_mg": totals[13],
            "vitamin_d_mcg": totals[14],
            "vitamin_b12_mcg": totals[15],
            "vitamin_c_mg": totals[16],
            "cholesterol_mg": totals[17]
        }
    }

def format_summary(summary: dict) -> str:
    """Format summary for display."""
    
    lines = [f"📊 Nutrition Summary for {summary['date']}", ""]
    
    if not summary["meals"]:
        lines.append("No meals logged for this date.")
        return "\n".join(lines)
    
    # Meals breakdown
    lines.append("**Meals:**")
    for meal in summary["meals"]:
        meal_type = (meal["type"] or "meal").capitalize()
        name = meal["name"] or "Unnamed"
        cals = meal["calories"] or 0
        lines.append(f"  • {meal_type}: {name} ({cals:.0f} cal)")
    
    lines.append("")
    
    # Daily totals
    t = summary["totals"]
    lines.append("**Daily Totals:**")
    lines.append(f"  Calories: {t['calories'] or 0:.0f}")
    lines.append(f"  Protein: {t['protein_g'] or 0:.1f}g")
    lines.append(f"  Carbs: {t['carbs_g'] or 0:.1f}g (fiber: {t['fiber_g'] or 0:.1f}g, sugar: {t['sugar_g'] or 0:.1f}g)")
    lines.append(f"  Fat: {t['fat_total_g'] or 0:.1f}g (sat: {t['fat_saturated_g'] or 0:.1f}g, unsat: {t['fat_unsaturated_g'] or 0:.1f}g)")
    lines.append("")
    lines.append("**Key Micronutrients:**")
    lines.append(f"  Sodium: {t['sodium_mg'] or 0:.0f}mg | Potassium: {t['potassium_mg'] or 0:.0f}mg")
    lines.append(f"  Calcium: {t['calcium_mg'] or 0:.0f}mg | Iron: {t['iron_mg'] or 0:.1f}mg | Magnesium: {t['magnesium_mg'] or 0:.0f}mg")
    lines.append(f"  Vitamin D: {t['vitamin_d_mcg'] or 0:.1f}mcg | B12: {t['vitamin_b12_mcg'] or 0:.1f}mcg | C: {t['vitamin_c_mg'] or 0:.0f}mg")
    lines.append(f"  Cholesterol: {t['cholesterol_mg'] or 0:.0f}mg")
    
    return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser(description="Nutrition summary")
    parser.add_argument("--today", action="store_true", help="Show today's summary")
    parser.add_argument("--date", help="Show summary for specific date (YYYY-MM-DD)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()
    
    if args.today:
        date_str = datetime.now().strftime("%Y-%m-%d")
    elif args.date:
        date_str = args.date
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")
    
    summary = get_daily_summary(date_str)
    
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(format_summary(summary))

if __name__ == "__main__":
    main()
