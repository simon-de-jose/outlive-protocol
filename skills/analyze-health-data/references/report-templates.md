# Report Templates

Reports stored in: `<data_dir>/reports/`
```
reports/
  baselines/<date>.md         # Initial baseline (user-specific)
  weekly/YYYY-WNN.md          # ISO week number
  monthly/YYYY-MM.md          # Monthly deep dive
```

## Weekly Report Template (Sunday 8 PM)

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
→ Delegate to **coach-cardio** skill for full logic, views, and benchmarks.
Use `v_cardio_fitness` (FTP W/kg) and `v_vo2max_trend` (VO2 max + ACSM classification).
If no cardio this week, note "No cardio sessions logged" and skip.

## 🏋️ Strength Training
→ Delegate to **coach-strength** skill for full logic, queries, and coaching.
Query `hevy_workouts`, `hevy_sets`, `coach_progression` tables.
If no workouts this week, note "No strength sessions logged" and skip.

## 🥗 Nutrition
→ Delegate to **coach-nutrition** skill for full logic, views, and benchmarks.
Use `v_daily_nutrition` (protein/kg tracking) and `v_meal_glucose_response` (CGM + meal correlation).
Key metrics: avg protein g/kg, days on target, avg glucose spike, worst/best meals.
If no meals logged this week, note "No meals logged" and skip.

## Highlights
- Best/worst days and why
- Notable correlations (meal → glucose, sleep → HRV, training → recovery, etc.)

## Recommendations
- 1-2 actionable items for next week
```

## Monthly Report Template (1st of month 8 PM)

Everything in the weekly template PLUS:
- Month-over-month trend analysis
- Body composition trajectory
- VO2 max / FTP progression
- Blood work integration (if new labs)
- Medication adherence
- Comparison to baseline
- **Exercise deep dive** — pull from coach-cardio (FTP/VO2 max trend) and coach-strength (e1RM curves, volume by muscle group, training load vs recovery)
- **Nutrition deep dive** — pull from coach-nutrition (protein trend, top glucose-friendly vs spiking meals, macro distribution, nutrition → sleep correlation)
