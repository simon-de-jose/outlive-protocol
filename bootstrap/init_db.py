#!/usr/bin/env python3
"""
Initialize the health database with schema.

Creates tables: readings, metrics, imports, medications, workouts.
Also creates views for cardio, nightly signals, and nutrition.

Usage:
    python -m bootstrap.init_db
"""

import duckdb
import sys
from pathlib import Path

from bootstrap.env import db_path

DB_PATH = db_path()

def init_database():
    """Create database and tables if they don't exist."""
    
    # Ensure data directory exists
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    # Connect to database (creates file if doesn't exist)
    conn = duckdb.connect(str(DB_PATH))
    
    try:
        # Create readings table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                timestamp TIMESTAMP NOT NULL,
                metric VARCHAR NOT NULL,
                value DOUBLE NOT NULL,
                unit VARCHAR NOT NULL,
                source VARCHAR NOT NULL,
                PRIMARY KEY (timestamp, metric, source)
            )
        """)
        
        # Create indexes for common query patterns
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_readings_timestamp 
            ON readings(timestamp)
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_readings_metric 
            ON readings(metric)
        """)
        
        # Create metrics metadata table
        conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS seq_metrics START 1
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                metric_id INTEGER PRIMARY KEY DEFAULT nextval('seq_metrics'),
                name VARCHAR UNIQUE NOT NULL,
                display_name VARCHAR,
                category VARCHAR,
                unit VARCHAR,
                description VARCHAR
            )
        """)
        
        # Create imports log table
        conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS seq_imports START 1
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS imports (
                import_id INTEGER PRIMARY KEY DEFAULT nextval('seq_imports'),
                filename VARCHAR UNIQUE NOT NULL,
                imported_at TIMESTAMP NOT NULL,
                rows_added INTEGER NOT NULL,
                source VARCHAR NOT NULL
            )
        """)
        
        # Create medications table
        conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS seq_medications START 1
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS medications (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_medications'),
                timestamp TIMESTAMP NOT NULL,
                scheduled_at TIMESTAMP,
                medication VARCHAR NOT NULL,
                dosage DOUBLE,
                scheduled_dosage DOUBLE,
                unit VARCHAR,
                status VARCHAR,
                UNIQUE(timestamp, medication)
            )
        """)
        
        # Create index for medications queries
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_medications_timestamp 
            ON medications(timestamp)
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_medications_medication 
            ON medications(medication)
        """)
        
        # Create workouts table
        conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS seq_workouts START 1
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workouts (
                id INTEGER PRIMARY KEY DEFAULT nextval('seq_workouts'),
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                type VARCHAR NOT NULL,
                duration_seconds INTEGER,
                total_energy_kcal DOUBLE,
                active_energy_kcal DOUBLE,
                max_heart_rate DOUBLE,
                avg_heart_rate DOUBLE,
                distance_km DOUBLE,
                step_count INTEGER,
                UNIQUE(start_time, type)
            )
        """)
        
        # Create index for workouts queries
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_workouts_start_time 
            ON workouts(start_time)
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_workouts_type 
            ON workouts(type)
        """)
        
        # Verify tables exist
        tables = conn.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'main'
        """).fetchall()
        
        table_names = [t[0] for t in tables]
        expected = ['readings', 'metrics', 'imports', 'medications', 'workouts']
        
        print(f"✅ Database initialized: {DB_PATH}")
        print(f"✅ Created tables: {', '.join(sorted(table_names))}")
        
        # Verify expected tables exist
        missing = set(expected) - set(table_names)
        if missing:
            print(f"⚠️  Warning: Expected tables not found: {', '.join(missing)}")
            return False
        
        return True
        
    except Exception as e:
        print(f"❌ Error initializing database: {e}")
        return False
    
    finally:
        conn.close()

if __name__ == "__main__":
    success = init_database()
    sys.exit(0 if success else 1)


def init_cardio_views():
    """Create cardio fitness tracking views."""
    conn = duckdb.connect(str(DB_PATH))
    try:
        # FTP W/kg with classification
        conn.execute('''
            CREATE OR REPLACE VIEW v_cardio_fitness AS
            WITH latest_weight AS (
                SELECT value * 0.453592 as weight_kg,
                       timestamp as measured_at
                FROM readings
                WHERE metric = 'Weight'
                ORDER BY timestamp DESC
                LIMIT 1
            ),
            ftp_history AS (
                SELECT
                    DATE(timestamp) as date,
                    value as ftp_watts,
                    timestamp
                FROM readings
                WHERE metric LIKE '%Threshold Power%'
            ),
            vo2_history AS (
                SELECT
                    DATE(timestamp) as date,
                    value as vo2max,
                    timestamp
                FROM readings
                WHERE metric LIKE '%VO2%'
            )
            SELECT
                f.date,
                f.ftp_watts,
                w.weight_kg,
                ROUND(f.ftp_watts / w.weight_kg, 2) as watts_per_kg,
                v.vo2max,
                CASE
                    WHEN f.ftp_watts / w.weight_kg >= 4.0 THEN 'elite'
                    WHEN f.ftp_watts / w.weight_kg >= 3.0 THEN 'very_good'
                    WHEN f.ftp_watts / w.weight_kg >= 2.0 THEN 'average_to_good'
                    ELSE 'below_average'
                END as ftp_class,
                CASE
                    WHEN v.vo2max >= 51.1 THEN 'superior'
                    WHEN v.vo2max >= 45.7 THEN 'excellent'
                    WHEN v.vo2max >= 42.4 THEN 'good'
                    WHEN v.vo2max >= 36.7 THEN 'fair'
                    ELSE 'poor'
                END as vo2_class
            FROM ftp_history f
            CROSS JOIN latest_weight w
            LEFT JOIN vo2_history v ON f.date = v.date
            ORDER BY f.date DESC
        ''')

        # VO2 max standalone trend
        conn.execute('''
            CREATE OR REPLACE VIEW v_vo2max_trend AS
            SELECT
                DATE(timestamp) as date,
                value as vo2max,
                CASE
                    WHEN value >= 51.1 THEN 'superior'
                    WHEN value >= 45.7 THEN 'excellent'
                    WHEN value >= 42.4 THEN 'good'
                    WHEN value >= 36.7 THEN 'fair'
                    ELSE 'poor'
                END as classification,
                ROUND(value / 55.0 * 100, 1) as pct_of_elite_target
            FROM readings
            WHERE metric LIKE '%VO2%'
            ORDER BY timestamp DESC
        ''')

        print("✅ Cardio fitness views created (v_cardio_fitness, v_vo2max_trend)")
    finally:
        conn.close()


def init_nightly_signals_view():
    """Create the nightly signals view (sleep + recovery metrics with z-scores)."""
    conn = duckdb.connect(str(DB_PATH))
    try:
        conn.execute('''
            CREATE OR REPLACE VIEW v_nightly_signals AS
            WITH nightly AS (
                SELECT
                    CAST(timestamp AS DATE) AS night,
                    MAX(CASE WHEN metric = 'Sleep Analysis [Total]' THEN value END) AS sleep_total,
                    MAX(CASE WHEN metric = 'Sleep Analysis [Deep]' THEN value END) AS deep_hrs,
                    MAX(CASE WHEN metric = 'Sleep Analysis [REM]' THEN value END) AS rem_hrs,
                    MAX(CASE WHEN metric = 'Sleep Analysis [Core]' THEN value END) AS core_hrs,
                    MAX(CASE WHEN metric = 'Sleep Analysis [Awake]' THEN value END) AS awake_hrs,
                    AVG(CASE WHEN metric = 'Heart Rate Variability' THEN value END) AS avg_hrv,
                    MAX(CASE WHEN metric = 'Resting Heart Rate' THEN value END) AS resting_hr,
                    AVG(CASE WHEN metric = 'Respiratory Rate' THEN value END) AS avg_resp_rate,
                    AVG(CASE WHEN metric = 'Apple Sleeping Wrist Temperature' THEN value END) AS wrist_temp_delta,
                    AVG(CASE WHEN metric = 'Blood Oxygen Saturation' THEN value END) AS avg_spo2,
                    MAX(CASE WHEN metric = 'Breathing Disturbances' THEN value END) AS breathing_disturbances
                FROM readings
                WHERE metric IN (
                    'Sleep Analysis [Total]', 'Sleep Analysis [Deep]', 'Sleep Analysis [REM]',
                    'Sleep Analysis [Core]', 'Sleep Analysis [Awake]', 'Heart Rate Variability',
                    'Resting Heart Rate', 'Respiratory Rate', 'Apple Sleeping Wrist Temperature',
                    'Blood Oxygen Saturation', 'Breathing Disturbances'
                )
                GROUP BY CAST(timestamp AS DATE)
                HAVING sleep_total IS NOT NULL
            ),
            with_pct AS (
                SELECT *,
                    ROUND(deep_hrs / NULLIF(sleep_total, 0) * 100, 1) AS deep_pct,
                    ROUND(rem_hrs / NULLIF(sleep_total, 0) * 100, 1) AS rem_pct,
                    ROUND(core_hrs / NULLIF(sleep_total, 0) * 100, 1) AS core_pct
                FROM nightly
            ),
            with_z AS (
                SELECT *,
                    -- z-scores vs 30-day rolling window
                    ROUND((sleep_total - AVG(sleep_total) OVER w30)
                        / NULLIF(STDDEV(sleep_total) OVER w30, 0), 2) AS z_sleep,
                    ROUND((avg_hrv - AVG(avg_hrv) OVER w30)
                        / NULLIF(STDDEV(avg_hrv) OVER w30, 0), 2) AS z_hrv,
                    ROUND((-1 * (resting_hr - AVG(resting_hr) OVER w30))
                        / NULLIF(STDDEV(resting_hr) OVER w30, 0), 2) AS z_rhr,
                    ROUND((deep_pct - AVG(deep_pct) OVER w30)
                        / NULLIF(STDDEV(deep_pct) OVER w30, 0), 2) AS z_deep,
                    ROUND((rem_pct - AVG(rem_pct) OVER w30)
                        / NULLIF(STDDEV(rem_pct) OVER w30, 0), 2) AS z_rem
                FROM with_pct
                WINDOW w30 AS (ORDER BY night ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
            )
            SELECT *,
                -- divergence: how "unusual" is this night vs rolling baseline
                ROUND(SQRT(
                    (POWER(COALESCE(z_sleep,0),2) + POWER(COALESCE(z_hrv,0),2)
                     + POWER(COALESCE(z_rhr,0),2) + POWER(COALESCE(z_deep,0),2)
                     + POWER(COALESCE(z_rem,0),2)) / 5.0
                    - POWER((COALESCE(z_sleep,0) + COALESCE(z_hrv,0) + COALESCE(z_rhr,0)
                             + COALESCE(z_deep,0) + COALESCE(z_rem,0)) / 5.0, 2)
                ), 2) AS divergence_score
            FROM with_z
        ''')
        print("✅ Nightly signals view created (v_nightly_signals)")
    finally:
        conn.close()


def init_nutrition_views():
    """Create nutrition coaching views."""
    conn = duckdb.connect(str(DB_PATH))
    try:
        # Daily nutrition summary with protein/kg
        conn.execute("""
            CREATE OR REPLACE VIEW v_daily_nutrition AS
            WITH daily AS (
                SELECT
                    DATE(meal_time) as date,
                    COUNT(*) as meals,
                    SUM(calories) as calories,
                    SUM(protein_g) as protein_g,
                    SUM(carbs_g) as carbs_g,
                    SUM(fat_total_g) as fat_g,
                    SUM(fiber_g) as fiber_g,
                    SUM(sugar_g) as sugar_g
                FROM nutrition_log
                GROUP BY DATE(meal_time)
            ),
            latest_weight AS (
                SELECT value * 0.453592 as weight_kg
                FROM readings
                WHERE metric = 'Weight'
                ORDER BY timestamp DESC
                LIMIT 1
            )
            SELECT
                d.*,
                ROUND(d.protein_g / w.weight_kg, 2) as protein_per_kg,
                CASE
                    WHEN d.protein_g / w.weight_kg >= 2.2 THEN 'high'
                    WHEN d.protein_g / w.weight_kg >= 1.6 THEN 'on_target'
                    WHEN d.protein_g / w.weight_kg >= 1.2 THEN 'borderline'
                    ELSE 'low'
                END as protein_status,
                w.weight_kg
            FROM daily d
            CROSS JOIN latest_weight w
            ORDER BY d.date DESC
        """)

        # Meal-glucose response correlation
        conn.execute("""
            CREATE OR REPLACE VIEW v_meal_glucose_response AS
            WITH meal_windows AS (
                SELECT entry_id, meal_time, meal_name, meal_type,
                       calories, carbs_g, fiber_g, sugar_g, protein_g
                FROM nutrition_log
            ),
            pre_meal AS (
                SELECT m.entry_id, AVG(r.value) as pre_meal_glucose
                FROM meal_windows m
                JOIN readings r ON r.metric IN ('Glucose (Historic)', 'Glucose (Scan)')
                    AND r.timestamp BETWEEN m.meal_time - INTERVAL 30 MINUTE AND m.meal_time
                GROUP BY m.entry_id
            ),
            post_meal AS (
                SELECT m.entry_id,
                    MAX(r.value) as peak_glucose,
                    (EXTRACT(EPOCH FROM (
                        MIN(CASE WHEN r.value = sub.max_val THEN r.timestamp END) - m.meal_time
                    )) / 60)::INT as time_to_peak_min
                FROM meal_windows m
                JOIN readings r ON r.metric IN ('Glucose (Historic)', 'Glucose (Scan)')
                    AND r.timestamp BETWEEN m.meal_time + INTERVAL 15 MINUTE
                    AND m.meal_time + INTERVAL 120 MINUTE
                JOIN (
                    SELECT m2.entry_id, MAX(r2.value) as max_val
                    FROM meal_windows m2
                    JOIN readings r2 ON r2.metric IN ('Glucose (Historic)', 'Glucose (Scan)')
                        AND r2.timestamp BETWEEN m2.meal_time + INTERVAL 15 MINUTE
                        AND m2.meal_time + INTERVAL 120 MINUTE
                    GROUP BY m2.entry_id
                ) sub ON sub.entry_id = m.entry_id
                GROUP BY m.entry_id, m.meal_time
            ),
            two_hr AS (
                SELECT DISTINCT ON (m.entry_id)
                    m.entry_id, r.value as glucose_2hr
                FROM meal_windows m
                JOIN readings r ON r.metric IN ('Glucose (Historic)', 'Glucose (Scan)')
                    AND r.timestamp BETWEEN m.meal_time + INTERVAL 100 MINUTE
                    AND m.meal_time + INTERVAL 140 MINUTE
                ORDER BY m.entry_id,
                    ABS(EXTRACT(EPOCH FROM (r.timestamp - (m.meal_time + INTERVAL 120 MINUTE))))
            )
            SELECT
                m.meal_time, m.meal_name, m.meal_type, m.calories,
                m.carbs_g, m.fiber_g, m.sugar_g, m.protein_g,
                ROUND(pre.pre_meal_glucose, 1) as pre_meal_glucose,
                ROUND(post.peak_glucose, 1) as peak_glucose,
                ROUND(post.peak_glucose - pre.pre_meal_glucose, 1) as glucose_spike,
                post.time_to_peak_min,
                ROUND(t.glucose_2hr, 1) as glucose_2hr,
                CASE
                    WHEN post.peak_glucose - pre.pre_meal_glucose > 50 THEN 'large'
                    WHEN post.peak_glucose - pre.pre_meal_glucose > 30 THEN 'significant'
                    WHEN post.peak_glucose - pre.pre_meal_glucose > 15 THEN 'moderate'
                    WHEN post.peak_glucose - pre.pre_meal_glucose IS NOT NULL THEN 'minimal'
                    ELSE NULL
                END as spike_class
            FROM meal_windows m
            LEFT JOIN pre_meal pre ON pre.entry_id = m.entry_id
            LEFT JOIN post_meal post ON post.entry_id = m.entry_id
            LEFT JOIN two_hr t ON t.entry_id = m.entry_id
            ORDER BY m.meal_time DESC
        """)

        print("✅ Nutrition coaching views created (v_daily_nutrition, v_meal_glucose_response)")
    finally:
        conn.close()
