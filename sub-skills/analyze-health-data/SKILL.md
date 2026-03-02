---
name: analyze-health-data
description: Personal health optimization inspired by Peter Attia's Outlive. Weekly/monthly health reviews, daily digest from longevity experts on X, and quick Q&A against Apple Health data in DuckDB. Covers sleep, glucose, cardiovascular risk, fitness, and body composition.
---

# analyze-health-data — Personal Health Optimization

> Framework: Peter Attia's Medicine 3.0 / Outlive
> Channel: #outlive (Discord, Apollo category)
> DB: Read from `config.yaml → data.db_path`

## Shared Config

- **Python:** Use the workspace venv (default: `~/<workspace>/.venv/bin/python`)
- **Scripts:** `../../scripts/` (relative to this skill)
- **Gurus list:** `../../data/gurus.json` (relative to repo root)
- **Digest state:** `../../data/digest-state.json` (relative to repo root)

## Overview

Two functions:
1. **Quick Q&A** — Juan asks health questions in #outlive, José queries the DB and answers
2. **Scheduled reviews** — Weekly (Sunday 8 PM) and monthly (1st of month 8 PM) deep dives

## The Four Horsemen (Attia Framework)
1. **Cardiovascular disease** — Track ApoB/LDL, BP, resting HR, HRV
2. **Cancer** — Early detection awareness, metabolic health (insulin/glucose)
3. **Metabolic disease** — CGM glucose control, A1c, insulin sensitivity (TG/HDL ratio)
4. **Neurodegenerative disease** — Sleep quality, exercise, metabolic health

## Five Tactical Pillars
1. **Exercise** — Zone 2 (3-4 hrs/wk), strength (3-4x/wk), VO2 max progression
2. **Nutrition** — Protein 1.6-2.2 g/kg/day, glucose-friendly meals
3. **Sleep** — 7-8.5 hrs, deep > 15%, REM > 20%, consistency
4. **Emotional health** — Not currently tracked in DB
5. **Pharmacology** — Medication adherence tracking

## Targets (Baseline: Feb 26, 2026)

### Body Composition
- Body fat: < 15% (baseline: 17.1%)
- Lean mass: trending up (baseline: 133 lb / 60.3 kg)
- Weight: secondary metric, monitor only

### Cardiovascular
- ApoB: < 60 mg/dL (not yet tested)
- LDL-C: < 100 mg/dL (baseline: 97 — use until ApoB available)
- BP: < 120/80 (baseline: 106/70 ✅)
- Resting HR: trending down (baseline: 59.7 bpm)
- HRV: trending up (baseline: 49 ms)

### Metabolic
- CGM avg glucose: < 100 mg/dL (baseline: 94.3 ✅)
- Glucose SD: < 15 mg/dL (baseline: 18.5 ⚠️)
- Time in range (70-110): > 90% (baseline: 82.9% ⚠️)
- A1c: < 5.3% (baseline: 5.3% ✅)

### Sleep
- Total: 7-8.5 hrs (baseline: 7.64 ✅)
- Deep: > 15% of total (baseline: 10.5% 🔴)
- REM: > 20% of total (baseline: 28% ✅)

### Fitness
- VO2 max: > 45 ml/kg/min (baseline: 40.1 ⚠️)
- Zone 2: ≥ 3 hrs/week (baseline: ~2 hrs)
- FTP: trending up (baseline: 148.6 W / 2.04 W/kg)
- Strength: 3-4 sessions/week
- Protein: 116-160g/day (1.6-2.2 g/kg)

## Database Schema

```sql
-- Main readings table (9.3M rows)
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
$VENV -c "
import sys; sys.path.insert(0, '$HOME/Projects/outlive-protocol/scripts')
from config import get_db_path
import duckdb
db = duckdb.connect(str(get_db_path()), read_only=True)
# your query here
db.close()
"
```

## Report Structure

Reports stored in: Read from `config.yaml → data.reports_dir`
```
reports/
  baselines/2026-02-26.md    # Initial baseline
  weekly/2026-W09.md         # ISO week number
  monthly/2026-02.md         # Monthly deep dive
```

### Weekly Report Template (Sunday 8 PM)

Post summary to #outlive, save full report to disk.

```markdown
# Outlive Weekly — Week {N}, {Year}

## Scorecard
| Category | Metric | Target | This Week | Trend | Status |
|----------|--------|--------|-----------|-------|--------|
| Sleep | Total | 7-8.5 hrs | X | ↑↓→ | ✅⚠️🔴 |
| Sleep | Deep % | > 15% | X | | |
| Sleep | REM % | > 20% | X | | |
| Glucose | Avg | < 100 | X | | |
| Glucose | TIR | > 90% | X | | |
| Glucose | SD | < 15 | X | | |
| Fitness | Zone 2 hrs | ≥ 3 | X | | |
| Fitness | Workouts | 4+/wk | X | | |
| Body | Fat % | < 15% | X | | |
| Body | Lean Mass | ↑ | X | | |
| Cardio | Resting HR | ↓ | X | | |
| Cardio | HRV | ↑ | X | | |

## Highlights
- Best/worst days and why
- Notable correlations (meal → glucose, sleep → HRV, etc.)

## Recommendations
- 1-2 actionable items for next week
```

### Monthly Report Template (1st of month 8 PM)

Everything in weekly PLUS:
- Month-over-month trend analysis
- Body composition trajectory
- VO2 max / FTP progression
- Blood work integration (if new labs)
- Medication adherence
- Comparison to baseline

## Quick Q&A Guidelines

When Juan asks a health question in #outlive:
1. Query the DuckDB for relevant data
2. Provide a concise, data-backed answer
3. **Actively reference Attia/Outlive concepts** — quote or cite specific ideas (NEAT, Zone 2, centenarian decathlon, glucose disposal, the four horsemen, etc.) when they connect to the data.
4. If data is unavailable, say so — never fabricate

## Zone Classification (Pending Max HR Test)

Until max HR test is done (~March 19, 2026):
- Use estimated max HR: 220 - age (need age confirmation)
- Zone 2 HR: roughly 60-70% of max HR
- Zone 2 power: roughly 55-75% of FTP (~82-111W)

After max HR test: update zones with actual values.

## Longevity Digest (daily cron: outlive-digest)

Script: `$REPO/scripts/` (see sync_libre.py and related)
Gurus: `$REPO/data/gurus.json`
State: `$REPO/data/digest-state.json`
