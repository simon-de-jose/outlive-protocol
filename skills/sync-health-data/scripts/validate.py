#!/usr/bin/env python3
"""
Data quality validation for health database.

Runs validation checks and anomaly detection without blocking imports.
Prints warnings for issues that need attention.

Usage:
    python src/validate.py
    python src/validate.py --verbose
"""

import duckdb
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from config import get_db_path

# Paths
DB_PATH = get_db_path()

# Validation thresholds
HEART_RATE_MIN = 30
HEART_RATE_MAX = 220
RESTING_HR_DEVIATION_THRESHOLD = 15  # bpm difference from 7-day average
ANOMALY_LOOKBACK_DAYS = 30  # Only check recent data for anomalies

class ValidationReport:
    """Track validation findings."""
    
    def __init__(self):
        self.warnings = []
        self.info = []
    
    def add_warning(self, message):
        self.warnings.append(message)
    
    def add_info(self, message):
        self.info.append(message)
    
    def has_issues(self):
        return len(self.warnings) > 0
    
    def print_report(self, verbose=False):
        """Print validation report."""
        print("\n" + "="*60)
        print("🔍 DATA QUALITY VALIDATION")
        print("="*60)
        
        if verbose and self.info:
            print("\n📋 Info:")
            for msg in self.info:
                print(f"   ℹ️  {msg}")
        
        if self.warnings:
            print(f"\n⚠️  Warnings ({len(self.warnings)}):")
            for msg in self.warnings:
                print(f"   ⚠️  {msg}")
        else:
            print("\n✅ No data quality issues found")
        
        print("="*60)

def validate_heart_rate_range(conn, report):
    """Check for heart rate readings outside normal range."""
    
    # Check for heart rate outliers
    # Exclude HRV (measured in ms) and Recovery (percentage)
    outliers = conn.execute(f"""
        SELECT timestamp, value, metric
        FROM readings
        WHERE metric LIKE '%Heart Rate%'
          AND metric NOT LIKE '%Variability%'
          AND metric NOT LIKE '%Recovery%'
          AND (value < {HEART_RATE_MIN} OR value > {HEART_RATE_MAX})
        ORDER BY timestamp DESC
        LIMIT 10
    """).fetchall()
    
    if outliers:
        report.add_warning(
            f"Found {len(outliers)} heart rate readings outside normal range ({HEART_RATE_MIN}-{HEART_RATE_MAX} bpm)"
        )
        for ts, value, metric in outliers[:3]:  # Show first 3
            report.add_warning(f"  {ts}: {metric} = {value} bpm")
        if len(outliers) > 3:
            report.add_warning(f"  ... and {len(outliers) - 3} more")
    else:
        report.add_info("Heart rate values within normal range")

def validate_no_future_timestamps(conn, report):
    """Check for timestamps in the future."""
    
    now = datetime.now()
    future_readings = conn.execute("""
        SELECT COUNT(*), MIN(timestamp) as earliest_future
        FROM readings
        WHERE timestamp > ?
    """, [now]).fetchone()
    
    count, earliest = future_readings
    
    if count > 0:
        report.add_warning(
            f"Found {count} readings with future timestamps (earliest: {earliest})"
        )
    else:
        report.add_info("No future timestamps found")

def validate_date_coverage(conn, report):
    """Check for missing days in the date range."""
    
    # Get date range
    date_range = conn.execute("""
        SELECT 
            MIN(DATE(timestamp)) as earliest,
            MAX(DATE(timestamp)) as latest,
            COUNT(DISTINCT DATE(timestamp)) as days_with_data
        FROM readings
    """).fetchone()
    
    earliest, latest, days_with_data = date_range
    
    if not earliest or not latest:
        report.add_warning("No data found in database")
        return
    
    # Calculate expected days (earliest/latest are already date objects from DuckDB)
    if isinstance(earliest, str):
        earliest_date = datetime.strptime(earliest, '%Y-%m-%d').date()
        latest_date = datetime.strptime(latest, '%Y-%m-%d').date()
    else:
        earliest_date = earliest
        latest_date = latest
    
    expected_days = (latest_date - earliest_date).days + 1
    
    missing_days = expected_days - days_with_data
    
    report.add_info(
        f"Date coverage: {earliest} to {latest} ({days_with_data}/{expected_days} days)"
    )
    
    if missing_days > 0:
        report.add_warning(f"Missing data for {missing_days} day(s) in date range")
        
        # Find specific missing dates (limit to first 5)
        missing_dates = conn.execute("""
            WITH date_series AS (
                SELECT DATE(MIN(timestamp)) + INTERVAL (n) DAY as date
                FROM readings, generate_series(0, 
                    DATE_DIFF('day', 
                        DATE(MIN(timestamp)), 
                        DATE(MAX(timestamp))
                    )
                ) as t(n)
            )
            SELECT date
            FROM date_series
            WHERE date NOT IN (SELECT DISTINCT DATE(timestamp) FROM readings)
            ORDER BY date
            LIMIT 5
        """).fetchall()
        
        if missing_dates:
            for (date,) in missing_dates:
                report.add_warning(f"  Missing: {date}")
            if missing_days > 5:
                report.add_warning(f"  ... and {missing_days - 5} more")

def detect_resting_hr_anomalies(conn, report):
    """Detect unusual resting heart rate values (recent data only)."""
    
    # Calculate lookback date
    lookback_date = (datetime.now() - timedelta(days=ANOMALY_LOOKBACK_DAYS)).strftime('%Y-%m-%d')
    
    # Get recent resting HR readings with 7-day rolling average
    anomalies = conn.execute(f"""
        WITH resting_hr AS (
            SELECT 
                DATE(timestamp) as date,
                AVG(value) as daily_avg
            FROM readings
            WHERE metric = 'Resting Heart Rate'
            GROUP BY DATE(timestamp)
        ),
        with_rolling_avg AS (
            SELECT
                date,
                daily_avg,
                AVG(daily_avg) OVER (
                    ORDER BY date 
                    ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING
                ) as rolling_avg_7d
            FROM resting_hr
        )
        SELECT 
            date,
            daily_avg,
            rolling_avg_7d,
            ABS(daily_avg - rolling_avg_7d) as deviation
        FROM with_rolling_avg
        WHERE rolling_avg_7d IS NOT NULL
          AND ABS(daily_avg - rolling_avg_7d) > {RESTING_HR_DEVIATION_THRESHOLD}
          AND date >= '{lookback_date}'
        ORDER BY date DESC
        LIMIT 5
    """).fetchall()
    
    if anomalies:
        report.add_warning(
            f"Found {len(anomalies)} day(s) with unusual resting heart rate in last {ANOMALY_LOOKBACK_DAYS} days (>{RESTING_HR_DEVIATION_THRESHOLD} bpm from 7-day avg)"
        )
        for date, daily, rolling, deviation in anomalies:
            report.add_warning(
                f"  {date}: {daily:.1f} bpm (7-day avg: {rolling:.1f} bpm, diff: {deviation:.1f})"
            )
    else:
        report.add_info(f"No resting heart rate anomalies in last {ANOMALY_LOOKBACK_DAYS} days")

def run_validation(verbose=False):
    """
    Run all validation checks.
    
    Args:
        verbose: Show info messages in addition to warnings
    
    Returns:
        ValidationReport: Report object with findings
    """
    
    if not DB_PATH.exists():
        print(f"❌ Database not found: {DB_PATH}")
        return None
    
    conn = duckdb.connect(str(DB_PATH))
    report = ValidationReport()
    
    try:
        # Run all validation checks
        validate_heart_rate_range(conn, report)
        validate_no_future_timestamps(conn, report)
        validate_date_coverage(conn, report)
        detect_resting_hr_anomalies(conn, report)
        
    finally:
        conn.close()
    
    return report

def main():
    parser = argparse.ArgumentParser(description="Health data quality validation")
    parser.add_argument("--verbose", action="store_true",
                       help="Show info messages in addition to warnings")
    args = parser.parse_args()
    
    report = run_validation(verbose=args.verbose)
    
    if report:
        report.print_report(verbose=args.verbose)
        return 0 if not report.has_issues() else 1
    else:
        return 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
