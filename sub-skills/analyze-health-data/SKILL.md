---
name: analyze-health-data
description: Personal health optimization inspired by Peter Attia's Outlive. Weekly/monthly health reviews, daily digest from longevity experts on X, and quick Q&A against Apple Health data in DuckDB. Covers sleep, glucose, cardiovascular risk, fitness, and body composition.
---

> **Path Resolution:** Run `bash ../../shell/paths.sh --json` to resolve all paths (`venv`, `scripts`, `data`, `db`, etc.)

# analyze-health-data — Personal Health Optimization

> Framework: Peter Attia's Medicine 3.0 / Outlive
> Channel: #outlive (Discord, Apollo category)
> DB: Read from `config.yaml → data.data_dir` (or `data.db_path`)


## Overview

Two functions:
1. **Quick Q&A** — the user asks health questions in #outlive, the assistant queries the DB and answers
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

## Targets

Read the user's baseline from: `<data_dir>/reports/baselines/`
If no baseline exists, prompt the user to run an initial baseline capture.

**Target ranges (generic Attia/Outlive framework):**

### Body Composition
- Body fat: < 15% for males, < 22% for females
- Lean mass: trending up
- Weight: secondary metric, monitor only

### Cardiovascular
- ApoB: < 60 mg/dL (gold standard for ASCVD risk)
- LDL-C: < 100 mg/dL (use until ApoB available)
- BP: < 120/80
- Resting HR: trending down
- HRV: trending up

### Metabolic
- CGM avg glucose: < 100 mg/dL
- Glucose SD: < 15 mg/dL
- Time in range (70-110): > 90%
- A1c: < 5.3%

### Sleep
- Total: 7-8.5 hrs
- Deep: > 15% of total
- REM: > 20% of total

### Fitness
- VO2 max: "exceptional" for age (see Attia's centenarian decathlon targets)
- Zone 2: ≥ 3 hrs/week
- Strength: 3-4 sessions/week
- Protein: 1.6-2.2 g/kg/day

User-specific targets, baselines, and progression are in the baseline file.

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
import sys; sys.path.insert(0, '<scripts>')  # resolve via shell/paths.sh
from config import get_db_path
import duckdb
db = duckdb.connect(str(get_db_path()), read_only=True)
# your query here
db.close()
"
```

## Report Structure

Reports stored in: `<data_dir>/reports/`
```
reports/
  baselines/<date>.md         # Initial baseline (user-specific)
  weekly/YYYY-WNN.md          # ISO week number
  monthly/YYYY-MM.md          # Monthly deep dive
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

## 🚴 Cardio
(Data from coach-cardio sub-skill — query workouts table + readings for HR)

| Metric | Target | This Week | Trend |
|--------|--------|-----------|-------|
| Zone 2 | ≥ 180 min | X min | ↑↓→ |
| Zone 2 sessions | 3-4 | X | |
| VO2 max sessions | 1-2 | X | |

List each cardio session with duration, avg HR, and zone classification.
If no cardio this week, note "No cardio sessions logged" and skip.

## 🏋️ Strength Training
(Data from coach-workout sub-skill — query hevy_workouts, hevy_sets, coach_progression)

| Metric | Target | This Week | Trend |
|--------|--------|-----------|-------|
| Sessions | per user plan | X | ↑↓→ |
| Total volume (kg) | ↑ | X,XXX | |
| PRs this week | — | X | |

### Exercise Highlights
For each exercise with data this week:
- 🟢 Progressing: weight or reps increased vs last session
- 🟡 Stalled: same weight/reps 3+ sessions
- 🔴 Regressing: volume or e1RM dropped

### Queries
```sql
-- Week's workout count
SELECT COUNT(*) FROM hevy_workouts
WHERE start_time >= DATE_TRUNC('week', CURRENT_DATE)

-- Week's total volume by exercise
SELECT s.exercise_name, SUM(s.weight_kg * s.reps) as volume
FROM hevy_sets s JOIN hevy_workouts w ON s.workout_id = w.id
WHERE w.start_time >= DATE_TRUNC('week', CURRENT_DATE)
  AND s.set_type = 'normal'
GROUP BY s.exercise_name ORDER BY volume DESC

-- e1RM progression (last 4 sessions per exercise)
SELECT exercise_template_id, date, estimated_1rm_kg, total_volume_kg
FROM coach_progression
ORDER BY exercise_template_id, date DESC
```

If no workouts this week, note "No strength sessions logged this week" and skip the section.

## Highlights
- Best/worst days and why
- Notable correlations (meal → glucose, sleep → HRV, training → recovery, etc.)

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
- **Strength progression curves** (e1RM trends per exercise over the month)
- **Volume by muscle group** balance analysis
- **Training load vs recovery** (cross-reference workout volume with HRV/sleep)
- **Training frequency adherence** (actual vs planned sessions)

## Quick Q&A Guidelines

When the user asks a health question in #outlive:
1. Query the DuckDB for relevant data
2. Provide a concise, data-backed answer
3. **Actively reference Attia/Outlive concepts** — quote or cite specific ideas (NEAT, Zone 2, centenarian decathlon, glucose disposal, the four horsemen, etc.) when they connect to the data.
4. If data is unavailable, say so — never fabricate

## Zone Classification

If max HR test has been done → use actual zones from the baseline file.
If not → estimate:
- Max HR ≈ 220 - age
- Zone 2 HR ≈ 60-70% of max HR
- Zone 2 power ≈ 55-75% of FTP

After max HR test: update the baseline file with actual values.

## Longevity Digest (daily cron: outlive-digest)

Gurus: `<data_dir>/gurus.json`
State: `<data_dir>/digest-state.json`
