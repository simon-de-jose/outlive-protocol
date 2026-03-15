---
name: coach-cardio
description: Zone 2 and VO2 max coaching from HealthKit workout data. Tracks cardio targets, HR zone classification, and recovery readiness. Integrates with weekly/monthly Outlive reports.
---

> **Path Resolution:** Run `bash ../../shell/paths.sh --json` to resolve all paths

# coach-cardio — Cardio Training Coach

> Data source: HealthKit workouts (via sync-health-data)
> Channel: #outlive (Discord)
> DB: `health.duckdb` (via config.yaml)

## Overview

Coaches the two cardio pillars from Attia's Outlive framework:
1. **Zone 2** — Low-intensity steady-state (3-4 hrs/week target)
2. **VO2 max intervals** — High-intensity (1-2 sessions/week target)

No external API needed — all data comes from HealthKit workouts already synced by sync-health-data.

## Data Sources

HealthKit provides workout data in the `workouts` table:
```sql
workouts(id, start_time, end_time, type, duration_seconds,
         total_energy_kcal, active_energy_kcal,
         max_heart_rate, avg_heart_rate, distance_km)
```

And heart rate readings in `readings`:
```sql
-- Per-minute HR during workouts
SELECT timestamp, value FROM readings
WHERE metric = 'Heart Rate'
  AND timestamp BETWEEN '<workout_start>' AND '<workout_end>'
```

## Zone Classification

Zones are based on max HR. Read from baseline file in `<data_dir>/reports/baselines/`.

If no max HR test done, estimate:
- **Max HR** ≈ 220 - age
- **Zone 1** (recovery): < 55% max HR
- **Zone 2** (aerobic base): 55-75% max HR — *this is the target zone*
- **Zone 3** (tempo): 75-85% max HR
- **Zone 4** (threshold): 85-95% max HR
- **Zone 5** (VO2 max): > 95% max HR

### Classifying a Workout

A workout is classified by where the user spent most of their time:

```sql
-- Get HR readings during a workout
SELECT value as hr FROM readings
WHERE metric = 'Heart Rate'
  AND timestamp BETWEEN '<start>' AND '<end>'
ORDER BY timestamp
```

- **Zone 2 session:** avg HR in Zone 2 range, duration ≥ 30 min
- **VO2 max session:** periods of Zone 4-5 HR interspersed with recovery (intervals)
- **Mixed/other:** everything else

### Using avg_heart_rate Shortcut

When per-minute HR isn't available, use the workout's `avg_heart_rate`:
```sql
SELECT type, duration_seconds, avg_heart_rate, max_heart_rate
FROM workouts
WHERE start_time >= DATE_TRUNC('week', CURRENT_DATE)
```

## Primary Metrics

### FTP / Watts per Kilo (W/kg)
The single best metric for Zone 2 fitness. Measures sustained aerobic power output normalized to body weight.

**View:** `v_cardio_fitness` — joins FTP, weight, and VO2 max with auto-classification.
```sql
SELECT date, ftp_watts, weight_kg, watts_per_kg, ftp_class
FROM v_cardio_fitness
ORDER BY date DESC LIMIT 10
```

**Benchmarks (Attia framework):**
| W/kg | Classification | Notes |
|------|---------------|-------|
| < 2.0 | Below average | Priority: build aerobic base |
| 2.0-3.0 | Average to good | Most recreational athletes |
| 3.0-4.0 | Very good | Serious endurance athlete |
| > 4.0 | Elite | Top-tier |

FTP improves by consistently riding/running in Zone 2. It's not about intensity — it's about time in zone.

### VO2 Max (ml/kg/min)
Strongest single predictor of all-cause mortality (Attia cites this repeatedly). Measured by Apple Watch, tracked in HealthKit.

**View:** `v_vo2max_trend` — with ACSM classification and % of elite target.
```sql
SELECT date, vo2max, classification, pct_of_elite_target
FROM v_vo2max_trend
ORDER BY date DESC LIMIT 10
```

**Benchmarks (ACSM, male 30-39):**
| VO2 Max | Classification | Mortality risk |
|---------|---------------|----------------|
| < 36.7 | Poor | 4× higher than superior |
| 36.7-42.3 | Fair | 2-3× higher |
| 42.4-45.6 | Good | 1.5-2× higher |
| 45.7-51.0 | Excellent | Baseline |
| > 51.1 | Superior | Lowest risk |

**Attia's centenarian decathlon target:** Top 2% for your age ≈ 55+ ml/kg/min for male 30-39. The `pct_of_elite_target` column shows progress toward this.

**How to improve:** VO2 max responds to BOTH Zone 2 volume AND high-intensity intervals. The protocol: 3-4 hrs/week Zone 2 + 1-2 sessions of 4×4 min intervals at 90-95% max HR.

## Weekly Targets (Attia Framework)

| Metric | Target | Why |
|--------|--------|-----|
| Zone 2 total | ≥ 3 hours/week | Mitochondrial efficiency, fat oxidation, metabolic health |
| Zone 2 sessions | 3-4 per week | Minimum effective dose, ~45-60 min each |
| VO2 max sessions | 1-2 per week | Strongest predictor of all-cause mortality |
| VO2 max intervals | 4×4 min at 90-95% max HR | Norwegian 4×4 protocol |

## Coaching Logic

### Weekly Assessment

```sql
-- This week's cardio workouts
SELECT type, duration_seconds/60.0 as minutes, avg_heart_rate, max_heart_rate,
       start_time
FROM workouts
WHERE start_time >= DATE_TRUNC('week', CURRENT_DATE)
  AND type IN ('Cycling', 'Running', 'Walking', 'Elliptical', 'Rowing',
               'Swimming', 'Stair Climbing', 'Hiking')
ORDER BY start_time
```

Classify each and calculate:
- Total Zone 2 minutes this week
- Number of VO2 max sessions
- Zone 2 deficit/surplus vs 180-min target

### Suggestions

**Under Zone 2 target:**
- "You've done X/180 min of Zone 2 this week. Need Y more minutes — a Z-min ride/walk would close the gap."
- If consistently under: "Consider adding a morning walk (45 min) or commute ride"

**No VO2 max sessions:**
- "No high-intensity work this week. Try 4×4 min intervals on the bike (90-95% max HR, 3 min recovery between)"

**Over-training signal:**
- Cross-reference with HRV:
  ```sql
  SELECT AVG(value) as avg_hrv FROM readings
  WHERE metric = 'Heart Rate Variability'
    AND timestamp > CURRENT_DATE - INTERVAL 7 DAY
  ```
- If HRV trending down + high training volume → "Recovery might be lagging. Consider an easy week."

**Zone drift:**
- If avg HR during "Zone 2" sessions is consistently in Zone 3 → "Your Zone 2 rides are too intense. Slow down — you should be able to hold a conversation."

## Report Integration

### Weekly Section (added to analyze-health-data report)
```markdown
## 🚴 Cardio

| Metric | Target | This Week | Trend |
|--------|--------|-----------|-------|
| Zone 2 | ≥ 180 min | X min | ↑↓→ |
| Zone 2 sessions | 3-4 | X | |
| VO2 max sessions | 1-2 | X | |
| Avg Z2 HR | Zone 2 range | X bpm | |

### Sessions
- Mon: 45 min cycling, avg 128 bpm (Zone 2 ✅)
- Wed: 30 min running, avg 155 bpm (Zone 3 ⚠️ — too intense for Z2)
- Fri: 4×4 min intervals, max 178 bpm (VO2 max ✅)
```

### Monthly Addition
- Zone 2 weekly totals trend (bar chart description)
- VO2 max progression (from HealthKit VO2 max readings)
- Resting HR trend (fitness proxy)
- Training volume vs HRV correlation

## Queries

### VO2 max trend
```sql
SELECT DATE(timestamp) as date, value
FROM readings
WHERE metric = 'VO2 Max (ml/(kg·min))'
ORDER BY date DESC LIMIT 30
```

### Resting HR trend (fitness proxy)
```sql
SELECT DATE(timestamp) as date, AVG(value) as rhr
FROM readings
WHERE metric = 'Resting Heart Rate'
GROUP BY date ORDER BY date DESC LIMIT 30
```

### Weekly Zone 2 totals (last 8 weeks)
```sql
SELECT DATE_TRUNC('week', start_time) as week,
       SUM(duration_seconds)/60.0 as total_minutes
FROM workouts
WHERE type IN ('Cycling', 'Running', 'Walking', 'Elliptical', 'Rowing')
  AND avg_heart_rate BETWEEN <zone2_low> AND <zone2_high>
  AND duration_seconds >= 1800  -- at least 30 min
GROUP BY week ORDER BY week DESC LIMIT 8
```

## No External API Needed

All data comes from HealthKit → sync-health-data → DuckDB. No additional setup beyond the existing health import pipeline.

## Onboarding

When user first asks about cardio coaching:
1. Check if max HR test has been done (baseline file)
2. If not, use estimated zones and note the limitation
3. Ask about current cardio habits and preferences (cycling, running, walking, swimming)
4. Store preferences in `<data_dir>/user-profile.yaml` under `cardio:` section:
   ```yaml
   cardio:
     preferred_activities: [cycling, walking]
     zone2_weekly_target_min: 180
     vo2max_sessions_target: 1
     max_hr: null  # set after test
   ```
