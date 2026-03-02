#!/usr/bin/env python3
"""
Sync glucose data from LibreLinkUp API into health database.

Uses pylibrelinkup to fetch recent glucose readings and imports them
with deduplication.

Requirements:
    pip install pylibrelinkup

Configuration:
    Store credentials in macOS Keychain:
    security add-generic-password -s "librelinkup" -a "email" -w "your@email.com"
    security add-generic-password -s "librelinkup" -a "password" -w "your_password"

Usage:
    python sync_libre.py              # Sync logbook (last ~2 weeks)
    python sync_libre.py --graph      # Sync graph (last 12 hours)
    python sync_libre.py --dry-run    # Preview without importing
"""

import argparse
import os
import sys
from pathlib import Path
from datetime import datetime
import subprocess

try:
    from pylibrelinkup import PyLibreLinkUp
except ImportError:
    print("❌ pylibrelinkup not installed. Run: pip install pylibrelinkup")
    sys.exit(1)

import duckdb

from config import get_db_path


def get_credentials() -> tuple[str, str]:
    """Get LibreLinkUp credentials from env vars or macOS keychain."""
    email = os.environ.get("LIBRELINKUP_EMAIL")
    password = os.environ.get("LIBRELINKUP_PASSWORD")
    
    if not email or not password:
        # Try macOS keychain
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "librelinkup", "-a", "password", "-w"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                password = result.stdout.strip()
            
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "librelinkup", "-a", "email", "-w"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                email = result.stdout.strip()
        except Exception:
            pass
    
    if not email or not password:
        raise ValueError(
            "LibreLinkUp credentials not found. Set LIBRELINKUP_EMAIL and "
            "LIBRELINKUP_PASSWORD environment variables, or store in macOS keychain:\n"
            '  security add-generic-password -s "librelinkup" -a "email" -w "your@email.com"\n'
            '  security add-generic-password -s "librelinkup" -a "password" -w "your_password"'
        )
    
    return email, password


def sync_libre(use_graph: bool = False, dry_run: bool = False) -> dict:
    """
    Sync glucose readings from LibreLinkUp API.
    
    Args:
        use_graph: If True, use graph() for 12h data. Otherwise logbook() for ~2 weeks.
        dry_run: If True, don't actually insert data.
    
    Returns:
        dict with sync statistics.
    """
    print("🔐 Authenticating with LibreLinkUp...")
    email, password = get_credentials()
    
    client = PyLibreLinkUp(email=email, password=password)
    client.authenticate()
    
    # Get patient list
    patients = client.get_patients()
    if not patients:
        print("⚠️  No patients found in LibreLinkUp account")
        return {"status": "no_patients"}
    
    print(f"👤 Found {len(patients)} patient(s)")
    
    # Find the right patient (Haishan, not Croissant 🥐)
    patient = None
    for p in patients:
        if "haishan" in p.first_name.lower() or "ye" in p.last_name.lower():
            if "croissant" not in p.first_name.lower():
                patient = p
                break
    
    if not patient:
        patient = patients[0]  # Fallback to first
    
    print(f"📍 Using patient: {patient.first_name} {patient.last_name}")
    
    # Fetch readings
    if use_graph:
        print("📊 Fetching graph data (last 12 hours)...")
        readings = client.graph(patient_identifier=patient)
    else:
        print("📖 Fetching logbook data (last ~2 weeks)...")
        readings = client.logbook(patient_identifier=patient)
    
    print(f"📥 Received {len(readings)} readings from API")
    
    if not readings:
        return {"status": "no_readings", "fetched": 0}
    
    if dry_run:
        print("🏃 Dry run — showing sample data:")
        for r in readings[:5]:
            print(f"  {r.timestamp}: {r.value} mg/dL")
        return {"status": "dry_run", "fetched": len(readings)}
    
    # Connect to database
    db_path = get_db_path()
    conn = duckdb.connect(str(db_path))
    
    # Insert readings with deduplication
    inserted = 0
    duplicates = 0
    
    for r in readings:
        metric = "Glucose (Scan)" if not use_graph else "Glucose (Historic)"
        
        try:
            conn.execute("""
                INSERT INTO readings (timestamp, metric, value, unit, source)
                VALUES (?, ?, ?, 'mg/dL', 'libre')
                ON CONFLICT (timestamp, metric, source) DO NOTHING
            """, [r.timestamp, metric, float(r.value)])
            inserted += 1
        except Exception as e:
            print(f"⚠️  Error inserting reading: {e}")
            duplicates += 1
    
    conn.commit()
    conn.close()
    
    print(f"✅ Synced {inserted} readings ({duplicates} errors/duplicates)")
    
    return {
        "status": "success",
        "fetched": len(readings),
        "inserted": inserted,
        "duplicates": duplicates
    }


def main():
    parser = argparse.ArgumentParser(description="Sync LibreLinkUp glucose data")
    parser.add_argument("--graph", action="store_true", 
                        help="Use graph data (12h) instead of logbook (~2 weeks)")
    parser.add_argument("--dry-run", action="store_true", 
                        help="Preview without importing")
    
    args = parser.parse_args()
    
    try:
        result = sync_libre(use_graph=args.graph, dry_run=args.dry_run)
        print(f"\n📋 Result: {result}")
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
