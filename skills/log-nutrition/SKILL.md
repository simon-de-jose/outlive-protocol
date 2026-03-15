---
name: log-nutrition
description: Log meals from photos or text descriptions into the health database. Uses USDA FoodData Central API for nutrient lookup, supports recipe management for repeated meals, and handles restaurant chain nutrition data. Use this skill whenever the user shares a meal photo, describes what they ate, wants to log food, build or edit a recipe, or asks "log this meal." Also triggers on messages in Nutrition Log threads in the #routine channel.
---

> **Path Resolution:** Run `bash ../../shell/paths.sh --json` to resolve all paths (`venv`, `scripts`, `data`, `db`, etc.)

# log-nutrition

Track meals from photos or text, query USDA FoodData Central API for full nutrient profiles, and store in DuckDB.

## User Defaults

Check `<data_dir>/user-profile.yaml` for a `nutrition_defaults` section.
If set, use those when the user gives ambiguous input (e.g. just "egg" or "coffee").
If not set, ask for clarification.

Common sensible defaults: egg = hard-boiled, coffee = black filtered (no milk/cream/sugar).

## Recipes

For creating, editing, and listing recipes, read `references/recipe-format.md`.

---

## Meal Logging Workflow

### Step 1: Infer Meal Timestamp ⏰
**Critical for glucose-meal correlation accuracy.**

The user often forgets to log meals in real-time. Use best judgment to set `meal_time`:

1. **If the user provides a time** → use it ("I had lunch at 12:30" → 12:30)
2. **If logging seems real-time** (message time falls within typical meal window below) → use message timestamp. If message time is **outside** the window, ask — even if it's close.
3. **If it seems late** — apply common sense:
   - "breakfast" logged at noon+ → probably eaten 7-9 AM, ask: "When did you have this? ~8 AM?"
   - "lunch" logged at 5 PM+ → probably eaten 12-1 PM, ask
   - "dinner" logged at 11 PM+ → probably eaten 6-8 PM, ask
   - "snack" → harder to guess, ask if >2 hrs seem off
4. **Typical meal windows** (user's pattern): Breakfast 7-9:30 AM, Lunch 11 AM-1 PM, Dinner 5-7 PM, Snacks variable

**Why this matters:** `v_meal_glucose_response` correlates meals with CGM glucose readings in the 15-120 min window after `meal_time`. A wrong timestamp means the glucose correlation is meaningless.

**When in doubt, ask.** A quick "When did you eat this?" is better than a silently wrong timestamp.

### Step 2: Check for Repeated Meals ⭐
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
3. Also check `<data>/recipes.json` for a recipe match → use recipe, ask "1 serving? Any changes today?"
4. If no match → continue to Step 3

### Step 3: Check Restaurant Nutrition (if applicable) 🍔
If the meal is from a **well-known restaurant chain**, look up their published nutrition data before falling back to USDA estimates.

**Known chains with published nutrition:** Panda Express, Chipotle, McDonald's, Chick-fil-A, Subway, Taco Bell, In-N-Out, Popeyes, Wendy's, Burger King, Starbucks, Sweetgreen, Cava, Wingstop, Five Guys, El Pollo Loco, The Habit, Jack in the Box, Carl's Jr., Del Taco, Raising Cane's, Shake Shack, etc.

**How:**
1. Web search: `"[restaurant name]" "[menu item]" nutrition facts site:[restaurant].com OR nutritionix.com`
2. Prefer the restaurant's own site (most accurate)
3. Set `source` to `'restaurant nutrition data'` in the DB insert

**⚠️ Browser efficiency:** Do NOT snapshot entire menu pages — use `browser act evaluate` with JS to extract only the items you need (~100 tokens vs 15-20k). For 3-4 items, web search is usually cheaper than any browser approach.

### Step 4: Resize Image (if photo)
⚠️ **MANDATORY** — Do NOT skip. Saves significant tokens.
```bash
cd <repo> && bash skills/log-nutrition/scripts/process_meal_photos.sh /path/to/image.jpg
```

### Step 5: Identify & Clarify
- Identify foods via vision/text (dishes, sides, sauces, beverages)
- Ask about: portion sizes, amount consumed, cooking method, ingredients for mixed dishes
- **Offer:** "Want to save this as a recipe for next time?" if it seems regular

### Step 6: USDA API Lookup
```bash
source <repo>/.env
# Search
curl -s "https://api.nal.usda.gov/fdc/v1/foods/search?api_key=$USDA_API_KEY&query=FOOD_NAME&pageSize=3"
# Detail by FDC ID
curl -s "https://api.nal.usda.gov/fdc/v1/food/FDC_ID?api_key=$USDA_API_KEY"
```

USDA data is per 100g — apply portion multipliers. Round: calories to whole number, macros to 1 decimal.

### Step 7: Calculate & Present
- Show per-item + total table
- **Simple/known items** (apple, banana, coffee, egg, items from recipes or previous logs): log immediately, no confirmation needed. Just show the summary after logging.
- **Complex/uncertain items** (new dishes, ambiguous portions, restaurant meals with unknowns): present the breakdown and ask to confirm before inserting.
- **Do NOT insert uncertain meals without confirmation.** For known items, log directly.

### Step 8: Insert to Database

For the full INSERT template and column reference, read `references/db-schema.md`.

### Step 9: Cleanup
Delete processed media after successful insert:
```bash
rm -f ~/.openclaw/media/inbound/<filename>
rm -f skills/log-nutrition/scripts/processed/<filename>
```

## Files
- `<data>/recipes.json` — Saved recipes with cached USDA nutrient data
- `skills/log-nutrition/scripts/process_meal_photos.sh` / `resize_image.sh` — Image resize scripts
- `.env` — USDA API key
