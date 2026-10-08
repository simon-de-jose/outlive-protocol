# cc-smoke-test: log-nutrition

Writes to the health DB. Verify entries land correctly; test with throwaway meals first.

## 1. Discovery
Ask: "Which skills are available?"
**Pass:** response lists `log-nutrition`.

## 2. Invocation — log a text meal
Prompt: "Log this meal: 2 eggs, 1 slice sourdough, half an avocado. Use log-nutrition."
**Pass:**
- CC invokes `log-nutrition`.
- USDA lookup runs, macros stored in DuckDB.
- Confirmation includes total protein/carbs/fat estimates.

## 3. Invocation — photo meal (if available)
Setup: place a meal photo at /tmp/meal.jpg.
Prompt: "Log this meal from /tmp/meal.jpg."
**Pass:** skill invoked; items parsed from photo + logged; CC surfaces uncertainty ("approximate portions") honestly.

## 4. Invocation — recipe
Prompt: "Create a recipe called 'my standard breakfast' with the ingredients from scenario 2."
**Pass:** recipe saved; next invocation can reference it by name.

## 5. Negative
Prompt: "How many calories in an apple?"
**Pass:** CC answers generically, does NOT invoke log-nutrition.

## Runs
<!-- date | pass/fail | notes -->
