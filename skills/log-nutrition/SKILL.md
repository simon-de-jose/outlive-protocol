---
name: log-nutrition
description: Log meals from photos or text descriptions. Uses USDA API for nutrient lookup and stores results in the health DuckDB database.
---

> **Path Resolution:** Run `bash ../../shell/paths.sh --json` to resolve all paths (`venv`, `scripts`, `data`, `db`, etc.)

# log-nutrition

Track meals from photos or text, query USDA FoodData Central API for full nutrient profiles, and store in DuckDB.


## User Defaults

Check `<data_dir>/user-profile.yaml` for a `nutrition_defaults` section.
If set, use those when the user gives ambiguous input (e.g. just "egg" or "coffee").
If not set, ask for clarification.

Common sensible defaults: egg = hard-boiled, coffee = black filtered (no milk/cream/sugar).

---

## Recipe Management

Recipes are saved ingredient lists with cached USDA data. They make logging repeated meals instant.

### Creating a Recipe

**Trigger:** "build a recipe," "save a recipe," "create recipe for X"

1. **Gather info:** Recipe name, servings (default: 1), optional notes
2. **Collect ingredients:** Name + amount in natural units, convert to grams for storage
   - Common: 1 cup milk = 244g, 1 large egg = 50g, 1 tbsp olive oil = 14g, 1 slice bread ≈ 30g
3. **USDA lookup per ingredient:** Search → pick best match (prefer "SR Legacy"/"Foundation") → get FDC ID → pull full nutrients
4. **Present:** Per-ingredient and per-serving totals table
5. **Confirm & save** to `<data>/recipes.json`

**Recipe JSON format:**
```json
{
  "id": "latte",
  "name": "Latte",
  "servings": 1,
  "notes": "Double shot, steamed whole milk",
  "created": "2026-02-08",
  "ingredients": [
    {
      "name": "Whole milk",
      "portion_g": 240,
      "usda_fdc_id": 746782,
      "nutrients_per_portion": {
        "calories": 149, "protein_g": 8.0, "carbs_g": 11.4,
        "fat_g": 7.9, "saturated_fat_g": 4.6,
        "fiber_g": 0, "sugar_g": 11.4, "sodium_mg": 105, "cholesterol_mg": 24
      }
    }
  ],
  "totals_per_serving": { "..." : "..." }
}
```

### Editing/Listing Recipes
- **Edit:** Load recipe → apply changes → re-lookup USDA if ingredient changed → recalculate → save
- **List:** Show all recipe names with brief description

---

## Meal Logging

### Step 0: Infer Meal Timestamp ⏰
**Critical for glucose-meal correlation accuracy.**

The user often forgets to log meals in real-time. Use best judgment to set `meal_time`:

1. **If the user provides a time** → use it ("I had lunch at 12:30" → 12:30)
2. **If logging seems real-time** (message time falls within typical meal window in #4) → use message timestamp. If message time is **outside** the window, ask — even if it's close.
3. **If it seems late** — apply common sense:
   - "breakfast" logged at noon+ → probably eaten 7-9 AM, ask: "When did you have this? ~8 AM?"
   - "lunch" logged at 5 PM+ → probably eaten 12-1 PM, ask
   - "dinner" logged at 11 PM+ → probably eaten 6-8 PM, ask
   - "snack" → harder to guess, ask if >2 hrs seem off
4. **Typical meal windows** (user's pattern):
   - Breakfast: 7-9:30 AM
   - Lunch: 11 AM-1 PM
   - Dinner: 5-7 PM
   - Snacks: variable

**Why this matters:** `v_meal_glucose_response` correlates meals with CGM glucose readings in the 15-120 min window after `meal_time`. A wrong timestamp means the glucose correlation is meaningless.

**When in doubt, ask.** A quick "When did you eat this?" is better than a silently wrong timestamp.

### Step 1: Check for Repeated Meals ⭐
Before any lookup, check if the user is referring to a previous meal:

**Trigger words:** "usual," "normal," "same as," "again," "like last time," "my go-to," or naming a combo you've logged before.

1. Query `nutrition_log` for recent matching entries:
   ```sql
   SELECT meal_name, food_items, calories, protein_g, carbs_g, fat_total_g, meal_time
   FROM nutrition_log
   WHERE meal_name ILIKE '%keyword%' OR food_items ILIKE '%keyword%'
   ORDER BY meal_time DESC LIMIT 5
   ```
2. If match found → reuse those exact nutrients, confirm with user, log directly
3. If no match → continue to Step 1b

### Step 1b: Check Recipes
Check `<data>/recipes.json` for a match:
- Match found → use recipe, ask "1 serving? Any changes today?"
- No match → continue to Step 2

### Step 2: Check Restaurant Nutrition (if applicable) 🍔
If the meal is from a **well-known restaurant chain**, look up their published nutrition data before falling back to USDA estimates.

**Known chains with published nutrition:** Panda Express, Chipotle, McDonald's, Chick-fil-A, Subway, Taco Bell, In-N-Out, Popeyes, Wendy's, Burger King, Starbucks, Sweetgreen, Cava, Wingstop, Five Guys, El Pollo Loco, The Habit, Jack in the Box, Carl's Jr., Del Taco, Raising Cane's, Shake Shack, etc.

**How:**
1. Web search: `"[restaurant name]" "[menu item]" nutrition facts site:[restaurant].com OR nutritionix.com`
2. Prefer the restaurant's own site (most accurate)
3. Fallback: Nutritionix, CalorieKing, or MyFitnessPal community entries
4. Set `source` to `'restaurant nutrition data'` in the DB insert

**⚠️ Browser efficiency for restaurant sites:**
- Do NOT snapshot entire menu pages — they return 15-20k tokens of DOM for all items.
- Use `browser act evaluate` with JS to extract only the items you need (~100 tokens).
- Rule of thumb: if you only need 3-4 items, web search is cheaper than any browser approach.

### Step 3: Resize Image (if photo)
⚠️ **MANDATORY** — Do NOT skip. Saves significant tokens.
```bash
cd <repo> && ./shell/process_meal_photos.sh /path/to/image.jpg
```

### Step 4: Identify & Clarify
- Identify foods via vision/text (dishes, sides, sauces, beverages)
- Ask about: portion sizes, amount consumed, cooking method, ingredients for mixed dishes
- **Offer:** "Want to save this as a recipe for next time?" if it seems regular

### Step 5: USDA API Lookup
```bash
source <repo>/.env
# Search
curl -s "https://api.nal.usda.gov/fdc/v1/foods/search?api_key=$USDA_API_KEY&query=FOOD_NAME&pageSize=3"
# Detail by FDC ID
curl -s "https://api.nal.usda.gov/fdc/v1/food/FDC_ID?api_key=$USDA_API_KEY"
```

**Required nutrients:**
- **Macros:** Energy (kcal), Protein, Carbs, Fat
- **Fat breakdown:** Saturated, Monounsaturated, Polyunsaturated
- **Other:** Fiber, Sugar, Sodium, Cholesterol
- **Optional micros:** Iron, Calcium, Potassium, B-12, Vitamin D

### Step 6: Calculate & Present
- USDA data is per 100g — apply portion multipliers
- Round: calories to whole number, macros to 1 decimal
- Show per-item + total table
- **Simple/known items** (e.g. apple, banana, coffee, egg, items from recipes or previous logs): log immediately, no confirmation needed. Just show the summary after logging.
- **Complex/uncertain items** (new dishes, ambiguous portions, restaurant meals with unknowns): present the breakdown and ask to confirm before inserting.
- **Do NOT insert uncertain meals without confirmation.** For known items, log directly.

### Step 7: Insert to Database

```python
# Use duckdb with the config module:
import sys; sys.path.insert(0, '<scripts>')  # resolve via shell/paths.sh
from config import get_db_path
import duckdb
db = duckdb.connect(str(get_db_path()))
```

**Table:** `nutrition_log` in health.duckdb

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

**Key notes:**
- `entry_id` → always use `nextval('seq_nutrition_id')`
- `food_items` → JSON string with name + portion_g per item
- `fat_unsaturated_g` → combined mono + poly
- `logged_at` → auto-fills with `CURRENT_TIMESTAMP`
- `source` → 'chat', 'voice memo', 'photo + conversation', etc.

### Step 8: Cleanup
Delete processed media after successful insert:
```bash
rm -f ~/.openclaw/media/inbound/<filename>
rm -f <shell>/processed/<filename>
```

---

## Lessons Learned
- ❌ Don't skip image resize → wastes tokens
- ❌ Don't query only basic macros → need full profile
- ❌ Don't use stale column names → always use the INSERT template above
- ✅ Clarifying questions flow works well
- ✅ USDA API is reliable
- ✅ Recipe matching saves time on repeated meals

## Files
- `data/recipes.json` — Saved recipes with cached USDA nutrient data
- `shell/process_meal_photos.sh` / `shell/resize_image.sh` — Image resize scripts
- `.env` — USDA API key
