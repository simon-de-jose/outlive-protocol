---
name: log-nutrition
description: Log meals from photos or text descriptions into the health database. Uses USDA FoodData Central API for nutrient lookup, supports recipe management for repeated meals, and handles restaurant chain nutrition data. Use this skill whenever the user shares a meal photo, describes what they ate, wants to log food, build or edit a recipe, or asks "log this meal." Also triggers on messages in Nutrition Log threads in the #routine channel.
---

> **Path Resolution:** Paths configured via `.env` at repo root. Python scripts use `bootstrap.env` module.

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

### Step 2: Check for Repeated / Similar Meals ⭐
Before any USDA lookup, check if the user is referring to a previous meal.

**Trigger words:** "usual," "normal," "same as," "again," "similar," "like last time," "my go-to," or naming a combo you've logged before.

1. Query `nutrition_log` for recent matching entries:
   ```sql
   SELECT meal_name, food_items, calories, protein_g, carbs_g, fat_total_g,
          fat_saturated_g, fat_unsaturated_g, fat_trans_g,
          fiber_g, sugar_g, sodium_mg, cholesterol_mg, meal_time
   FROM nutrition_log
   WHERE meal_name ILIKE '%keyword%' OR food_items ILIKE '%keyword%'
   ORDER BY meal_time DESC LIMIT 5
   ```

2. **Three reuse paths** (choose the right one):

   **A. Exact reuse** — user says "same as yesterday" / "same breakfast" / "again":
   → Copy ALL nutrient values verbatim from the matched entry. No USDA lookup. No schema read. Just INSERT with new timestamp.
   Fast-path INSERT template (all columns inline — do NOT read db-schema.md for this):
   ```sql
   INSERT INTO nutrition_log (
     entry_id, meal_time, meal_type, meal_name, food_items,
     calories, protein_g, carbs_g,
     fat_total_g, fat_saturated_g, fat_unsaturated_g, fat_trans_g,
     fiber_g, sugar_g, sodium_mg, cholesterol_mg,
     source, notes
   ) VALUES (
     nextval('seq_nutrition_entry'),
     '<new_timestamp>',
     '<meal_type>',
     '<reused_meal_name>',
     '<reused_food_items_json>',
     <reused_calories>, <reused_protein>, <reused_carbs>,
     <reused_fat_total>, <reused_fat_sat>, <reused_fat_unsat>, <reused_fat_trans>,
     <reused_fiber>, <reused_sugar>, <reused_sodium>, <reused_cholesterol>,
     'chat — same as <previous_date>',
     NULL
   );
   ```
   **This path should take exactly 3 steps:** query DB → INSERT → post confirmation.

   **B. Delta reuse** — user says "similar" but with ingredient swaps or removals:
   → Use the matched entry as a **baseline**. Apply arithmetic deltas:
   1. Start with the baseline's total nutrients
   2. **Subtract** removed ingredients (use their per-item nutrients from `food_items` JSON + USDA per-100g values)
   3. **Add** new/changed ingredients (USDA lookup ONLY for the new items — not the unchanged ones)
   4. This is much faster than re-looking up every ingredient from scratch.
   Example: Mar 30 entry had egg + tofu skin. Today has no egg + momen tofu instead.
   → Subtract egg contribution (~78 cal), subtract tofu skin (~63 cal), add momen tofu USDA lookup → done.
   **Only USDA-lookup the changed ingredients.** Reuse everything else from the baseline.

   **C. No match** → continue to Step 3 (full lookup pipeline).

3. Also check the `recipes` table in DuckDB for a recipe match → use recipe, ask "1 serving? Any changes today?" Fall back to `<data>/recipes.json` only if the DB table is empty.

### Step 2.5: Check Grocery Inventory (homemade meals only) 🏠
**Optional step — only applies when ALL conditions are met:**
1. The meal is clearly **homemade** (user lists raw ingredients, no restaurant/chain name mentioned)
2. A grocery `inventory.json` exists at `~/clawd/skills/grocery/inventory.json`
3. The inventory has matching items

**If any condition is NOT met, skip this step entirely.** The skill works exactly as before without inventory. This keeps the skill portable — users without grocery tracking get the same experience.

**How to use inventory for portion inference:**
1. Read `inventory.json` → check `.items` for each ingredient the user mentioned
2. Fuzzy-match ingredient names (e.g., "chicken" → `ground_chicken`, "tofu" → `tofu_momen`).
   Use the helper module at `scripts/inventory.py` — `match_ingredient(mention, items)` returns a `MatchResult` with the matched `InventoryItem` and a `suggestion` string.
3. For matched items, suggest portions based on available stock:
   - **Weight-based:** "Ground chicken — you have 1.08 lbs on hand. Used all of it, or how much?"
   - **Unit-based:** "Tofu — you have 2 blocks. Used one? Half?"
   - **Small remainder (< 0.1 lbs or < 1 unit):** Auto-suggest "used all" — e.g., "0.15 lbs chicken left — used it all?"
4. Parse the user's confirmation with `parse_confirmation(text, item)` — accepts "1 block", "half", "all", "2 packs", "0.5 lbs", bare numbers, etc.
5. For items NOT in inventory → fall through to Step 5 (Identify & Clarify) and Step 6 (USDA lookup) as normal
6. **Recipe matches (Step 2) always take priority** — if a recipe matched, use recipe portions, don't check inventory

**The inventory check replaces the "ask about portion sizes" conversation for matched items.** Instead of "how much chicken did you use?", you get "you have 1.08 lbs — used all? half?" — one question with context instead of an open-ended one.

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
> **Note:** These scripts use `sips` (macOS built-in). On Linux, use ImageMagick instead:
> `convert input.jpg -resize 1920x1920\> -quality 85 output.jpg`

### Step 5: Identify & Clarify
- Identify foods via vision/text (dishes, sides, sauces, beverages)
- Ask about: portion sizes, amount consumed, cooking method, ingredients for mixed dishes
- **Offer:** "Want to save this as a recipe for next time?" if it seems regular

### Step 6: USDA API Lookup

**Cache check first:**
```python
from scripts.usda_cache import get_or_fetch
nutrients = get_or_fetch(fdc_id)  # returns dict with keys: protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg, cholesterol_mg, calories
```
If cache miss → calls USDA API, stores result, returns. If cache hit → instant return, zero API calls.

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
- When building the `food_items` JSON array, include each item's portion, source identifier when known, calories, and per-item macros. Do not store item rows with only calories. Example:
  ```json
  {"item": "chicken breast", "portion": "6oz", "fdc_id": "171077", "calories": 280, "protein_g": 52.0, "carbs_g": 0, "fat_g": 6.2}
  ```
- **Simple/known items** (apple, banana, coffee, egg, items from recipes or previous logs): log immediately, no confirmation needed. Just show the summary after logging.
- **Complex/uncertain items** (new dishes, ambiguous portions, restaurant meals with unknowns): present the breakdown and ask to confirm before inserting.
- **Do NOT insert uncertain meals without confirmation.** For known items, log directly.

### Step 8: Insert to Database

Use the INSERT template from Step 2A above (it has all the columns you need).
For exact column types or micronutrient columns (potassium, calcium, iron, etc.), read `references/db-schema.md`.
**For repeated/delta meals, you already have the template — do NOT re-read the schema.**

### Step 8.5: Update Grocery Inventory (if applicable) 📦
**Only runs when Step 2.5 was used** (homemade meal with inventory-matched ingredients).

After the meal is successfully logged to the nutrition DB:
1. Use `subtract_inventory(items_dict, consumptions)` from `scripts/inventory.py` to subtract consumed amounts
   - `consumptions` is a list of `(inventory_key, amount)` tuples
   - The function floors at zero (never negative), accumulates multiple consumptions of the same key, and marks items as depleted
2. If any item is flagged `depleted`, alert: "{item name} is now out — add to shopping list?"
3. Update `inventory.json` with the new quantities

**Example:**
- Before: `ground_chicken.quantity = 1.08` lbs
- Meal used: 0.5 lbs
- After: `ground_chicken.quantity = 0.58` lbs

**Rules:**
- Only subtract for ingredients matched via inventory in Step 2.5
- Skip for restaurant meals, recipes (unless recipe ingredients also happen to be in inventory), or items not in inventory
- This is automatic — don't ask the user "should I update inventory?" Just do it and show the result.

### Step 9: Cleanup
Delete processed media after successful insert:
```bash
rm -f ~/.openclaw/media/inbound/<filename>
rm -f skills/log-nutrition/scripts/processed/<filename>
```

---

## Batch Meal Logging

Support logging multiple meals in a single message. The user logs all meals at once (typically at night).

**Format:** The user writes something like:
```
meals today: breakfast — eggs, toast. lunch — leftover chicken rice. dinner — salmon, rice, salad
```

Or numbered, or with newlines — any clear separation by meal is fine.

### How to Process
1. **Split by meal markers:** Look for breakfast/lunch/dinner/snack keywords, or numbered items, or clear separators
2. **Assign default timestamps** (since the user is logging at night, not in real-time):
   - Breakfast → ~8:00 AM
   - Lunch → ~12:00 PM
   - Dinner → ~6:30 PM
   - Snack → ask, or use midpoint between adjacent meals
3. **Process each meal through the same workflow:** Steps 2 → 2.5 → 3 → 5 → 6 → 7 → 8 → 8.5
   - Inventory checks apply to ALL meals in the batch (and subtraction accumulates — if meal 1 uses 0.5 lbs chicken, meal 2 sees 0.58 lbs remaining, not 1.08)
4. **Show combined summary** — one table with all meals, then total for the day
5. **Inventory subtraction happens once at the end** for all meals combined

### Example Flow
> **User:** "meals today: breakfast — 2 eggs, toast with butter. dinner — ground chicken, rice, tofu, broccoli with soy sauce"
>
> **José:** (checks inventory for chicken, rice, tofu → suggests portions)
> "Here's what I've got:
>
> | Meal | Food | Portion | Cal | Protein |
> |------|------|---------|-----|---------|
> | Breakfast (8 AM) | 2 eggs, hard-boiled | 100g | 155 | 12.6g |
> | Breakfast (8 AM) | Toast + butter | 44g | 165 | 3.1g |
> | Dinner (6:30 PM) | Ground chicken | 0.5 lbs (227g) | 239 | 27.1g |
> | ... | ... | ... | ... | ... |
> | **Total** | | | **1,247** | **82.3g** |
>
> 📦 Inventory: ground chicken 1.08 → 0.58 lbs, tofu 2 → 1 block, rice 5 → 4.6 lbs
>
> Logged ✅"

---

## Output Discipline

The user should only see the final "Logged ✅" message with the formatted nutrition table. Everything else is internal.

- **NEVER** send intermediate reasoning to the chat. No "Now let me...", "Good, API works", "Let me search for...", etc.
- **NEVER** send debugging messages or API status updates
- All USDA lookups, DB queries, recipe checks, and calculations happen silently via tool calls
- Only the final formatted result (the nutrition table + "Logged ✅") goes to the chat
- If a lookup fails, handle it internally. Don't narrate the failure to the user unless you need their input

## Scope Boundary

**This skill logs meals to `nutrition_log` in DuckDB. That's it.**

Do NOT:
- Write to knowledge-base journals or chronicles (that's the KB skill's job)
- Update any files outside the nutrition database and `inventory.json`

If other skills need to know about meal activity, they query `nutrition_log` themselves. This skill's job ends at the INSERT + Discord confirmation.

## Files
- `recipes` table in DuckDB — Saved recipes with cached USDA nutrient data (primary)
- `<data>/recipes.json` — Legacy recipe storage (migrate to DB if found)
- `skills/log-nutrition/scripts/process_meal_photos.sh` / `resize_image.sh` — Image resize scripts
- `.env` — USDA API key
