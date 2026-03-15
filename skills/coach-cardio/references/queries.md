# Coach Cardio — Queries & Benchmarks

## Benchmark Tables

### W/kg Classification (Attia Framework)
| W/kg | Classification | Notes |
|------|---------------|-------|
| < 2.0 | Below average | Priority: build aerobic base |
| 2.0-3.0 | Average to good | Most recreational athletes |
| 3.0-4.0 | Very good | Serious endurance athlete |
| > 4.0 | Elite | Top-tier |

### ACSM VO2 Max (Male 30-39)
| VO2 Max | Classification | Mortality risk |
|---------|---------------|----------------|
| < 36.7 | Poor | 4× higher than superior |
| 36.7-42.3 | Fair | 2-3× higher |
| 42.4-45.6 | Good | 1.5-2× higher |
| 45.7-51.0 | Excellent | Baseline |
| > 51.1 | Superior | Lowest risk |

**Attia's centenarian decathlon target:** Top 2% for your age ≈ 55+ ml/kg/min for male 30-39.

## Queries

### VO2 Max Trend
```sql
SELECT DATE(timestamp) as date, value
FROM readings
WHERE metric = 'VO2 Max (ml/(kg·min))'
ORDER BY date DESC LIMIT 30
```

### Resting HR Trend (Fitness Proxy)
```sql
SELECT DATE(timestamp) as date, AVG(value) as rhr
FROM readings
WHERE metric = 'Resting Heart Rate'
GROUP BY date ORDER BY date DESC LIMIT 30
```

### Weekly Zone 2 Totals (Last 8 Weeks)
```sql
SELECT DATE_TRUNC('week', start_time) as week,
       SUM(duration_seconds)/60.0 as total_minutes
FROM workouts
WHERE type IN ('Cycling', 'Running', 'Walking', 'Elliptical', 'Rowing')
  AND avg_heart_rate BETWEEN <zone2_low> AND <zone2_high>
  AND duration_seconds >= 1800  -- at least 30 min
GROUP BY week ORDER BY week DESC LIMIT 8
```

### This Week's Cardio Workouts
```sql
SELECT type, duration_seconds/60.0 as minutes, avg_heart_rate, max_heart_rate,
       start_time
FROM workouts
WHERE start_time >= DATE_TRUNC('week', CURRENT_DATE)
  AND type IN ('Cycling', 'Running', 'Walking', 'Elliptical', 'Rowing',
               'Swimming', 'Stair Climbing', 'Hiking')
ORDER BY start_time
```

### HRV for Recovery Check
```sql
SELECT AVG(value) as avg_hrv FROM readings
WHERE metric = 'Heart Rate Variability'
  AND timestamp > CURRENT_DATE - INTERVAL 7 DAY
```
