---
name: analyze-health-data
description: Health analytics and reporting using Peter Attia's Outlive framework. Handles quick Q&A against Apple Health data in DuckDB, weekly reports (Sunday 8 PM), and monthly deep dives. Use this skill whenever the user asks about their health data, sleep, glucose, heart rate, HRV, body composition, VO2 max trends, or wants a health summary. Also use for any question about Attia's framework, the Four Horsemen, or longevity metrics.
---

> **Path Resolution:** Run `bash ../../shell/paths.sh --json` to resolve all paths (`venv`, `scripts`, `data`, `db`, etc.)

# analyze-health-data — Personal Health Optimization

> Framework: Peter Attia's Medicine 3.0 / Outlive — for details, read `references/attia-framework.md`
> Channel: #outlive (Discord, Apollo category)
> DB: Read from `config.yaml → data.data_dir` (or `data.db_path`)

## Overview

Two functions:
1. **Quick Q&A** — the user asks health questions in #outlive, the assistant queries the DB and answers
2. **Scheduled reviews** — Weekly (Sunday 8 PM) and monthly (1st of month 8 PM) deep dives

## Targets

Read the user's baseline from: `<data_dir>/reports/baselines/`
If no baseline exists, prompt the user to run an initial baseline capture.

For generic target ranges, read `references/target-ranges.md`.

## Database Schema

```sql
-- Main readings table
readings(timestamp, metric, value, unit, source)

-- Key metrics: 'Weight', 'Body Fat Percentage', 'Lean Body Mass',
-- 'Heart Rate Variability', 'Resting Heart Rate', 'VO2 Max (ml/(kg·min))',
-- 'Glucose (Historic)', 'Glucose (Scan)', 'Sleep Analysis [Total|Deep|REM|Awake|Core]',
-- 'Blood Pressure [Systolic|Diastolic]', 'Cycling Functional Threshold Power',
-- Blood work: 'LDL Cholesterol', 'HDL Cholesterol', 'Triglycerides', 'Hemoglobin A1c', etc.

workouts(id, start_time, end_time, type, duration_seconds, total_energy_kcal,
         active_energy_kcal, max_heart_rate, avg_heart_rate, distance_km)

nutrition_log(entry_id, meal_time, meal_type, meal_name, meal_description,
              food_items, calories, protein_g, carbs_g, fat_total_g)

medications(id, timestamp, scheduled_at, medication, dosage, scheduled_dosage, unit, status)
```

## Querying the DB

```bash
<venv> -c "
import sys; sys.path.insert(0, 'skills/sync-health-data/scripts')
from config import get_db_path
import duckdb
db = duckdb.connect(str(get_db_path()), read_only=True)
# your query here
db.close()
"
```

Any skill's `scripts/config.py` can be used for `get_db_path()` — they all resolve to the same DB.

## Reports

For full report templates (weekly + monthly), read `references/report-templates.md`.

**Delegation pattern:** This skill owns the report *structure*. Domain-specific sections are delegated:
- 🚴 Cardio → **coach-cardio** skill (views: `v_cardio_fitness`, `v_vo2max_trend`)
- 🏋️ Strength → **coach-strength** skill (tables: `hevy_workouts`, `hevy_sets`, `coach_progression`)
- 🥗 Nutrition → **coach-nutrition** skill (views: `v_daily_nutrition`, `v_meal_glucose_response`)

## Quick Q&A Guidelines

When the user asks a health question in #outlive:
1. Query the DuckDB for relevant data
2. Provide a concise, data-backed answer
3. **Reference Attia/Outlive concepts when relevant** — but ONLY attribute claims to Attia if you have a specific source (podcast episode, book chapter, blog post). If you're connecting dots yourself, say "this is a general physiological principle" or "this is my interpretation" — never frame your own reasoning as "Attia says X."
4. If data is unavailable, say so — never fabricate
5. **No false attribution.** General physiology ≠ Attia said it. Be honest about what's established science, what's Attia's specific take, and what's your own inference.

## Longevity Digest (daily cron: outlive-digest)

Gurus: `<data_dir>/gurus.json`
State: `<data_dir>/digest-state.json`
