---
name: coach-nutrition
description: Nutrition coaching based on Attia's framework. Protein adequacy tracking, meal-glucose correlation (CGM + meal log), macronutrient quality analysis. Pairs with log-nutrition (input) skill.
---

> **Path Resolution:** Run `bash ../../shell/paths.sh --json` to resolve all paths

# coach-nutrition — Nutrition Coach

> Framework: Peter Attia's nutritional biochemistry approach
> Data sources: `nutrition_log` (meals) + `readings` (CGM glucose, weight)
> Channel: #outlive (Discord)
> DB: `health.duckdb` (via config.yaml)

## Overview

Three coaching areas:
1. **Protein adequacy** — Attia's #1 nutrition priority
2. **Glucose-meal correlation** — Which meals spike you? (CGM + meal timestamps)
3. **Macronutrient quality** — Fiber:sugar ratio, fat composition, caloric balance

Input comes from `log-nutrition` (meal logging + USDA lookups). This skill is the *analysis* layer.

## Primary Metric: Protein (g/kg/day)

Attia's protein target: **1.6-2.2 g/kg lean body mass/day** (or total body weight as approximation).

**View:** `v_daily_nutrition`
```sql
SELECT * FROM v_daily_nutrition ORDER BY date DESC LIMIT 7
```

Columns: `date`, `meals`, `calories`, `protein_g`, `carbs_g`, `fat_g`, `fiber_g`, `sugar_g`, `protein_per_kg`, `protein_status`

Classification:
| Protein g/kg | Status | Action |
|-------------|--------|--------|
| < 1.2 | 🔴 Low | Flag — needs attention |
| 1.2-1.59 | 🟡 Borderline | Suggest adding a protein-rich meal/snack |
| 1.6-2.2 | 🟢 On target | Attia range |
| > 2.2 | ⚪ High | Fine for most people, note it |

### Weekly Protein Summary
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

## Glucose-Meal Correlation

The killer feature: connect CGM spikes to specific meals.

**View:** `v_meal_glucose_response`
```sql
SELECT * FROM v_meal_glucose_response ORDER BY meal_time DESC LIMIT 10
```

Columns: `meal_time`, `meal_name`, `meal_type`, `calories`, `carbs_g`, `fiber_g`, `sugar_g`,
`pre_meal_glucose`, `peak_glucose`, `glucose_spike`, `time_to_peak_min`, `glucose_2hr`, `spike_class`

### How it works
1. **Pre-meal baseline:** Average glucose in 30 min before meal
2. **Peak glucose:** Maximum glucose in 15-120 min after meal
3. **Spike:** Peak minus baseline
4. **2-hr glucose:** Reading at ~120 min (recovery)

### Spike Classification (Attia framework)
| Spike (mg/dL) | Classification | Notes |
|--------------|----------------|-------|
| < 15 | 🟢 Minimal | Excellent glucose disposal |
| 15-30 | 🟡 Moderate | Normal, acceptable |
| 30-50 | 🟠 Significant | Review meal composition |
| > 50 | 🔴 Large | Problematic — high glycemic load |

### Coaching Actions
- **Large spike + high carbs, low fiber:** "This meal spiked you 45 mg/dL. The carb:fiber ratio of 8:1 suggests adding more fiber or reducing refined carbs."
- **Minimal spike + high carbs:** "Rice bowl only spiked 12 mg/dL — good glucose disposal, likely post-exercise window."
- **Pattern detection:** If same meal type consistently spikes → flag it. If same meal is fine post-exercise but bad sedentary → note the pattern.

### Best/Worst Meals
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

## Macronutrient Quality

### Fiber:Sugar Ratio
Attia emphasizes fiber intake. Target: > 25g/day fiber, fiber:sugar ratio > 1:2.

### Fat Composition
Track saturated vs unsaturated fat split. Available in `nutrition_log` columns:
`fat_saturated_g`, `fat_unsaturated_g`, `fat_trans_g`

### Micronutrients to Watch
Columns available: `sodium_mg`, `potassium_mg`, `calcium_mg`, `iron_mg`, `magnesium_mg`, `vitamin_d_mcg`, `vitamin_b12_mcg`, `vitamin_c_mg`

Key Attia-relevant ones:
- **Magnesium** — often deficient, affects sleep/HRV (cross-reference with v_nightly_signals)
- **Vitamin D** — immune function, bone health
- **Sodium/Potassium** — electrolyte balance (especially if exercising heavily)

## Report Integration

### Weekly Section
```markdown
## 🥗 Nutrition

| Metric | Target | This Week | Trend |
|--------|--------|-----------|-------|
| Avg calories | per plan | X | ↑↓→ |
| Protein | 1.6-2.2 g/kg | X g/kg | |
| Days on protein target | 7/7 | X/Y | |
| Fiber | > 25g/day | X g | |
| Avg glucose spike | < 30 mg/dL | X mg/dL | |

### Glucose-Meal Insights
- 🟢 Best meal: [meal] — avg spike X mg/dL
- 🔴 Worst meal: [meal] — avg spike X mg/dL
- Pattern: [any detected patterns]
```

If no meals logged this week, note "No meals logged" and skip.

### Monthly Addition
- Protein trend (daily g/kg over the month)
- Top 5 glucose-friendly vs glucose-spiking meals
- Macronutrient distribution trend
- Correlation: nutrition quality → sleep quality (magnesium, late eating)
- Correlation: meal timing → glucose response (post-exercise meals vs sedentary)

## Cross-Skill Correlations

The real power comes from connecting nutrition to other data:

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

## Onboarding

When user first asks about nutrition coaching:
1. Check how many days of meal data exist
2. If < 7 days: "Need at least a week of meal logging for meaningful analysis. Use the log-nutrition skill to log meals."
3. If >= 7 days: Generate first nutrition report with protein adequacy + glucose correlation
4. Store preferences in `<data_dir>/user-profile.yaml` under `nutrition:` section:
   ```yaml
   nutrition:
     protein_target_g_per_kg: 1.8  # middle of Attia range
     calorie_target: null          # set if user has a goal
     dietary_restrictions: []
   ```
