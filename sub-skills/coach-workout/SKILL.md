---
name: coach-workout
description: Strength training coach integrated with Hevy. Syncs workout data, tracks progressive overload, analyzes performance, and manages routines. Local DB is source of truth, Hevy is the sync target.
---

> **Path Resolution:** Run `bash ../../shell/paths.sh --json` to resolve all paths (`venv`, `scripts`, `data`, `db`, etc.)

# coach-workout — Strength Training Coach

> Integration: Hevy API (v1, requires Pro)
> Channel: #outlive (Discord)
> DB: `health.duckdb` (via config.yaml)

## Overview

Three modes:
1. **Sync** — Pull workout data from Hevy into local DB
2. **Analyze** — Post-workout analysis with progressive overload tracking
3. **Coach** — Suggest routine adjustments based on performance

**Source of truth:** Local `coach_routines` table. Hevy is where the user logs workouts; we sync FROM Hevy and push routine updates TO Hevy (with user confirmation).

## Data Flow

```
User logs workout in Hevy app
    ↓
sync_hevy.py (cron or on-demand)
    ↓
hevy_workouts + hevy_sets (local DB)
    ↓
coach_progression (calculated)
    ↓
Analysis + Coaching suggestions → #outlive
    ↓
Routine updates → push to Hevy (after user confirms)
```

## Database Tables

```sql
-- Exercise catalog (synced from Hevy, 400+ templates)
hevy_exercises(template_id, title, type, primary_muscle_group, secondary_muscle_groups, is_custom)

-- Workout sessions (synced from Hevy)
hevy_workouts(id, title, routine_id, start_time, end_time, duration_seconds, ...)

-- Individual sets
hevy_sets(id, workout_id, exercise_template_id, exercise_name,
          set_index, set_type, weight_kg, reps, rpe, ...)

-- Local routine definitions (SoT)
coach_routines(id, hevy_routine_id, title, split_type, day_label, exercises)
  -- exercises = JSON: [{exercise_template_id, title, sets: [{weight_kg, reps, ...}], notes}]

-- Progressive overload tracking (auto-calculated)
coach_progression(id, exercise_template_id, date,
                  estimated_1rm_kg, best_set_weight_kg, best_set_reps,
                  total_volume_kg, total_sets)
```

## Scripts

### Sync
```bash
<venv> <scripts>/sync_hevy.py              # Incremental sync
<venv> <scripts>/sync_hevy.py --backfill   # Full backfill (first run)
<venv> <scripts>/sync_hevy.py --dry-run    # Preview
<venv> <scripts>/sync_hevy.py --exercises  # Exercise catalog only
<venv> <scripts>/sync_hevy.py --routines   # Routines only
```

### Init (first time)
```bash
<venv> <scripts>/init_hevy.py
```

## Post-Workout Analysis

When a sync detects new completed workouts, analyze each one:

### 1. Volume Comparison
```sql
-- Compare today's workout vs last time same routine was done
SELECT s.exercise_name,
       SUM(s.weight_kg * s.reps) as volume_today
FROM hevy_sets s
JOIN hevy_workouts w ON s.workout_id = w.id
WHERE w.id = '<new_workout_id>'
  AND s.set_type = 'normal'
GROUP BY s.exercise_name
```

Compare against previous workout with same routine_id.

### 2. Progressive Overload Check
```sql
-- e1RM trend for an exercise (Epley formula)
SELECT date, estimated_1rm_kg, total_volume_kg
FROM coach_progression
WHERE exercise_template_id = '<template_id>'
ORDER BY date DESC
LIMIT 8
```

Flags:
- **PR** → New highest e1RM or volume
- **Progressing** → e1RM trending up over last 4 sessions
- **Stalled** → Same weight/reps for 3+ sessions
- **Regressing** → e1RM trending down

### 3. Compliance Check
Compare exercises completed vs routine definition in `coach_routines`.

## Coaching Logic

### When to suggest weight increase
- User completed all target reps at target RPE (7-8) for 2 consecutive sessions
- Increase: 2.5 kg for compounds, 1-2 kg for isolation

### When to suggest deload
- e1RM dropped 2+ sessions in a row
- User reports RPE 9-10 consistently
- Cross-reference with sleep/HRV data (from other sub-skills):
  ```sql
  SELECT AVG(value) as avg_hrv
  FROM readings
  WHERE metric = 'Heart Rate Variability'
    AND timestamp > NOW() - INTERVAL 7 DAY
  ```

### When to modify routine
- Exercise stalled 4+ weeks → suggest variation (e.g. DB bench → incline DB bench)
- Muscle group imbalance (compare volume across groups)
- User request

**Always ask before pushing changes to Hevy.**

## Routine Management

### Viewing current routine
```sql
SELECT id, title, hevy_routine_id, exercises
FROM coach_routines
ORDER BY title
```

### Pushing updates to Hevy
Use PUT `/v1/routines/{routineId}` with the updated exercise list.
**Requires explicit user confirmation.**

## Integration with Weekly/Monthly Reports

Add to the Outlive weekly report (analyze-health-data skill):

### Weekly Workout Section
```markdown
## 🏋️ Strength Training

| Metric | Target | This Week | Trend |
|--------|--------|-----------|-------|
| Sessions | 3-4/week | X | ↑↓→ |
| Total volume (kg) | ↑ | X,XXX | |
| PRs this week | — | X | |

### Exercise Highlights
- 🟢 Bench Press: 30lb × 10 → 35lb × 10 (↑ 17% e1RM)
- 🟡 OHP: 20lb × 10 × 3 sessions (stalled — suggest 22.5lb)
- 🔴 Bulgarian Split Squat: volume dropped 15% — check recovery
```

### Monthly Addition
- Strength progression curves (e1RM over time)
- Volume by muscle group balance chart
- Training frequency adherence
- Correlation: training load vs HRV/sleep

## Onboarding (New Users)

When no `coach_routines` exist:

1. Ask training experience (beginner/intermediate/advanced)
2. Ask available equipment (full gym / home dumbbells / bodyweight)
3. Ask training days per week (3-6)
4. Ask goals (strength / hypertrophy / general fitness / longevity)
5. Ask injuries/limitations
6. Generate routine based on answers + Attia's framework
7. Present for review → store locally → sync to Hevy

Store answers in `<data_dir>/user-profile.yaml` under `training:` section.

## Hevy API Reference

Auth: `api-key` header with key from `.env` (`HEVY_API_KEY`).

| Endpoint | Method | Use |
|----------|--------|-----|
| `/v1/workouts` | GET | Paginated workout list |
| `/v1/workouts` | POST | Create workout |
| `/v1/workouts/{id}` | PUT | Update workout |
| `/v1/workouts/events` | GET | Incremental sync since date |
| `/v1/routines` | GET/POST | List/create routines |
| `/v1/routines/{id}` | PUT | Update routine |
| `/v1/exercise_templates` | GET | Exercise catalog |
| `/v1/exercise_history/{id}` | GET | Per-exercise history |

## Troubleshooting

**"HEVY_API_KEY not found"**
- Add to `.env`: `HEVY_API_KEY=your_key_here`
- Get key at https://hevy.com/settings?developer (requires Hevy Pro)

**Sync shows 0 workouts**
- User needs to log workouts in the Hevy app first
- Check: `curl -H "api-key: $KEY" https://api.hevyapp.com/v1/workouts/count`

**Routine push failed**
- Verify routine exists in Hevy: check `hevy_routine_id` in `coach_routines`
- API may rate-limit — wait and retry
