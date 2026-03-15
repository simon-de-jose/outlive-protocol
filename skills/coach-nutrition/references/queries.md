# Coach Nutrition — Queries & Correlations

## Best/Worst Meals (by glucose spike)

```sql
-- Meals with lowest average spike (personal "safe foods")
SELECT meal_name, AVG(glucose_spike) as avg_spike, COUNT(*) as times
FROM v_meal_glucose_response
WHERE glucose_spike IS NOT NULL
GROUP BY meal_name HAVING COUNT(*) >= 2
ORDER BY avg_spike ASC LIMIT 10

-- Meals with highest average spike
SELECT meal_name, AVG(glucose_spike) as avg_spike, COUNT(*) as times
FROM v_meal_glucose_response
WHERE glucose_spike IS NOT NULL
GROUP BY meal_name HAVING COUNT(*) >= 2
ORDER BY avg_spike DESC LIMIT 10
```

## Weekly Protein Summary

```sql
SELECT
    DATE_TRUNC('week', date) as week,
    AVG(protein_g) as avg_daily_protein,
    AVG(protein_per_kg) as avg_g_per_kg,
    SUM(CASE WHEN protein_status = 'on_target' THEN 1 ELSE 0 END) as days_on_target,
    COUNT(*) as days_logged
FROM v_daily_nutrition
GROUP BY week ORDER BY week DESC LIMIT 4
```

## Cross-Skill Correlations

The real power comes from connecting nutrition to other data. These are aspirational — use when enough data exists.

### Nutrition → Sleep (v_nightly_signals)
- Late meals (after 8 PM) → sleep quality impact?
- Magnesium intake → HRV correlation?
- Alcohol (if tracked) → deep sleep impact?

### Nutrition → Glucose (readings)
- Post-exercise meals → lower spikes? (check workout timing from hevy_workouts)
- Time-of-day patterns (morning insulin resistance is common)

### Nutrition → Training (coach-strength)
- Protein on training days vs rest days
- Pre-workout nutrition → performance?
