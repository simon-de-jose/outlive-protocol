# Nutrition Skill Benchmark

Deterministic tests that verify the log-nutrition skill works correctly without calling an LLM or live USDA APIs.

## What it tests

| Suite | What it checks |
|-------|---------------|
| **Nutrient ID Mapping** | USDA cache maps nutrient IDs to correct field names (the big bug) |
| **Sequence Name** | SKILL.md uses `seq_nutrition_entry`, not `seq_nutrition_id` |
| **Output Discipline** | SKILL.md has rules to suppress intermediate chat messages |
| **Recipe Seeded** | The example breakfast recipe exists with per-item macros + correct totals |
| **Same Breakfast Repeat** | "Same breakfast" resolves from previous log, zero API calls, <30s |
| **Cache Correctness** | Cached foods have non-null carbs/protein/fat/calories |
| **Chinese Ingredients** | Cache hit rate ≥50% for common Chinese cooking ingredients |

## How to run

```bash
cd ~/Projects/outlive-protocol
python skills/log-nutrition/evals/run_benchmark.py
```

Exit code 0 = all pass, non-zero = failures.

Results are written to `evals/results.json`.

## Dry-run a fake meal log end-to-end

This simulates inventory-aware meal logging without touching the production DB or `inventory.json`.
It creates a temporary DuckDB, inserts a synthetic `nutrition_log` row there, and applies inventory subtraction only to an in-memory copy.

```bash
cd ~/Projects/outlive-protocol
python skills/log-nutrition/evals/dry_run_meal.py --example stir_fry
```

Or pass a custom payload:

```bash
python skills/log-nutrition/evals/dry_run_meal.py --json '{
  "meal": {"meal_time": "2026-04-12T18:30:00", "meal_type": "dinner", "meal_name": "Test meal", "food_items": [], "calories": 100, "source": "dry-run"},
  "inventory_confirmations": {"ground chicken": "1 pack", "tofu": "half"}
}'
```

## How to interpret results

- **PASS** = assertion met
- **FAIL** = something broken, check the evidence field for details
- Look at `evals/results.json` for the full structured output

## Adding new test cases

1. Add a test case to `evals.json` with prompt + assertions
2. Add a corresponding test function in `run_benchmark.py`
3. Register it in the `suites` list in `main()`

The benchmark uses a temporary DuckDB that's created fresh each run — no risk to production data.
