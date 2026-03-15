# Nutrition Log — DB Schema & INSERT Template

## Table: `nutrition_log` in health.duckdb

```sql
INSERT INTO nutrition_log (
  entry_id, meal_time, meal_type, meal_name, meal_description,
  food_items,
  calories, protein_g, carbs_g,
  fat_total_g, fat_saturated_g, fat_unsaturated_g, fat_trans_g,
  fiber_g, sugar_g, sodium_mg, cholesterol_mg,
  potassium_mg, calcium_mg, iron_mg, magnesium_mg,
  vitamin_d_mcg, vitamin_b12_mcg, vitamin_c_mg,
  source, notes
) VALUES (
  nextval('seq_nutrition_id'),    -- auto-generated, don't hardcode
  '2026-02-09 09:30',            -- meal_time (TIMESTAMP)
  'breakfast',                    -- meal_type (breakfast/lunch/dinner/snack)
  'Egg, baguette & avocado',     -- meal_name
  NULL,                           -- meal_description (optional)
  '[{"name":"Egg","portion_g":50,"fdc_id":173424}]',  -- JSON string
  256, 11.4, 18.5,               -- calories, protein, carbs
  15.8, 3.5, 10.8, 0,            -- fat_total, saturated, unsaturated (mono+poly combined), trans
  5.1, 1.2, 233, 186,            -- fiber, sugar, sodium, cholesterol
  NULL, NULL, NULL, NULL,         -- potassium, calcium, iron, magnesium
  NULL, NULL, NULL,               -- vitamin D, B12, C
  'photo + conversation',         -- source
  NULL                            -- notes
);
```

## Key Notes
- `entry_id` → always use `nextval('seq_nutrition_id')`
- `food_items` → JSON string with name + portion_g per item
- `fat_unsaturated_g` → combined mono + poly
- `logged_at` → auto-fills with `CURRENT_TIMESTAMP`
- `source` → 'chat', 'voice memo', 'photo + conversation', etc.

## Connecting to the DB

```python
import sys; sys.path.insert(0, 'skills/log-nutrition/scripts')
from config import get_db_path
import duckdb
db = duckdb.connect(str(get_db_path()))
```

## Required Nutrients from USDA

- **Macros:** Energy (kcal), Protein, Carbs, Fat
- **Fat breakdown:** Saturated, Monounsaturated, Polyunsaturated
- **Other:** Fiber, Sugar, Sodium, Cholesterol
- **Optional micros:** Iron, Calcium, Potassium, B-12, Vitamin D
