# Nutrition Log — Schema & Shared Writer Contract

## Table: `nutrition_log` in health.duckdb

The table contains the meal fields and nutrients listed below. **Never insert
into it with direct SQL or `nextval`.** Run the explicit migration, then use
the shared writer; it handles locking, schema validation, safe allocation and
atomic delivery idempotency.

```bash
python skills/log-nutrition/scripts/nutrition_migrate.py
python skills/log-nutrition/scripts/log_nutrition.py --json '{
  "meal_time":"2026-02-09T09:30:00",
  "meal_type":"breakfast",
  "meal_name":"Egg, baguette & avocado",
  "food_items":[{"name":"Egg","portion_g":50,"fdc_id":"173424"}],
  "calories":256,"protein_g":11.4,"carbs_g":18.5,"fat_total_g":15.8,
  "source":"photo + conversation",
  "provider":"discord","message_id":"<message_id>"
}'
```

## Key Notes
- **Do not execute this SQL directly.** All production writes must use the shared CLI, which validates the versioned schema, serializes writers, allocates ids safely, and records atomic idempotency receipts:
  ```bash
  python skills/log-nutrition/scripts/nutrition_migrate.py
  python skills/log-nutrition/scripts/log_nutrition.py --json '<payload>'
  ```
  See `ingest-migration.md` for the migration and structured `provider`/`message_id` identity contract.
- `food_items` → JSON string with name + portion_g per item
- `fat_unsaturated_g` → combined mono + poly
- `logged_at` → auto-fills with `CURRENT_TIMESTAMP`
- `source` → 'chat', 'voice memo', 'photo + conversation', etc.

## Connecting to the DB

```python
from bootstrap.env import db_path
import duckdb
db = duckdb.connect(str(db_path()), read_only=True)
```

## Required Nutrients from USDA

- **Macros:** Energy (kcal), Protein, Carbs, Fat
- **Fat breakdown:** Saturated, Monounsaturated, Polyunsaturated
- **Other:** Fiber, Sugar, Sodium, Cholesterol
- **Optional micros:** Iron, Calcium, Potassium, B-12, Vitamin D
