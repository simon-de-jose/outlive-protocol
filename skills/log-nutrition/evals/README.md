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
cd <repo>
python skills/log-nutrition/evals/run_benchmark.py
```

Exit code 0 = all pass, non-zero = failures.

Results print to stdout by default and leave tracked `evals/results.json` byte-identical. Pass `--write-results` to update the tracked file intentionally, or `--results-path /tmp/nutrition-results.json` for an explicit output file.

## Step 4 deterministic before/after benchmark

`step4_benchmark.py` is the reproducible deterministic portion of the Step 4 replay gate. It covers and scores the existing 11 replay fixtures, 20 retrieval fixtures, and 3 P0 executable writer fixtures using only temporary DuckDB files.

It compares:

- **old_pre_retrieval_lookup** — a faithfully labeled frozen old operation: SQL `ILIKE` history/recipe lookup only. It is counted as a local retrieval call when it performs lookup. It does not invent a parser, agent decisions, USDA/web calls, or writes.
- **current_nutrition_retrieve + shared writer** — read-only local retrieval for replay/retrieval fixtures, plus the central ingest writer for executable P0 fixtures.

```bash
cd <repo>
python skills/log-nutrition/evals/step4_benchmark.py --results-path /tmp/step4-results.json
```

By default it prints a summary and does **not** write tracked results. Use `--results-path` for an explicit artifact, or `--write-results` only when intentionally updating `evals/step4_results.json`.

The report includes p50/p95/p99 microbench latency with sample counts, retrieval calls, writes, duplicates, decision/candidate/replay correctness as explicit numerator+denominator fields, observed vs expected clarification/pass-through counts, unsafe fuzzy writes, recipe precedence errors, local deterministic network call counts, and a before/after SHA-256 proof that the live DB is unchanged. The live DB host path is intentionally omitted from reports.

Semantics enforced by the tests:

- Retrieval-call totals cover scored replay/retrieval decision operations only. Timing-loop calls are excluded from these totals and represented only by latency sample counts.
- Writer latency runs every P0 executable case for every `--repetitions` value on fresh isolated DBs, so the writer latency sample count is `p0_executable * repetitions`. P0 correctness/writes/duplicates are scored once per fixture to avoid inflating decision denominators.
- Replay scoring is explicit for all 11 replay fixtures. Clarification-like expected outcomes are observed as `needs_agent_decision`; photo workflow is observed separately as `pass_through`; delivery replay/idempotency fixtures are linked to isolated P0 writer evidence rather than guessed from text retrieval.
- Decision correctness only counts cases with explicit decision expectations: replay fixtures, retrieval fixtures with `top_action`, and P0 writer fixtures. Retrieval fixtures without `top_action` are excluded from `decision_total` and scored under candidate correctness only.
- Local benchmark network calls are reported as `0` because the harness does not use the network. Full-agent USDA/web/API calls are reported as `null`/unavailable with reasons, not as deterministic zero.

## Step 4 isolated full-agent A/B replay report

`step4_full_agent_report.py` turns completed isolated full-agent runs into a sanitized, reproducible scoring artifact. It consumes an existing run directory only; it does **not** rerun agents and it does not write tracked results by default.

```bash
cd <repo>
python skills/log-nutrition/evals/step4_full_agent_report.py /tmp/food-journal-step4-full-agent/runs
python skills/log-nutrition/evals/step4_full_agent_report.py /tmp/food-journal-step4-full-agent/runs --results-path /tmp/step4-full-agent-report.json
python -m pytest skills/log-nutrition/tests/test_step4_full_agent_report.py
```

The checked-in manifest at `evals/fixtures/step4_full_agent_manifest.json` contains sanitized exact case prompts and expectations only. Reports include per-path latency sample_count+p50/p95/p99, model rounds, tool calls by type from assistant `toolCall` records, explicit retrieval/USDA/web/write tool-call categories from tool-call arguments, DB writes, duplicates, case correctness, clarification/pass-through behavior, false confirmations, row provider/message-id integrity, receipt-table identity integrity, source/provenance integrity, and explicit limitations. The old brand row is treated as a truthful write when a matching row exists, but as a separate sequence/identity safety failure if it uses anomalous/manual ID allocation or lacks durable identity receipts. Raw trajectories, encrypted reasoning, production paths, local host paths, and private DB paths are intentionally omitted. Interpret old/new comparisons as small-n replay evidence; old-baseline contamination and trajectory-specific agent choices are reported limitations, not population estimates.

## Dry-run a fake meal log end-to-end

This simulates inventory-aware meal logging without touching the production DB or `inventory.json`.
It creates a temporary DuckDB, inserts a synthetic `nutrition_log` row there, and applies inventory subtraction only to an in-memory copy.

```bash
cd <repo>
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
