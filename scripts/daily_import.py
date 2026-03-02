#!/usr/bin/env python3
"""
Daily health data import job.

Scans the iCloud folder for Health Auto Export CSV files and imports any
that haven't been processed yet. Safe to run multiple times (idempotent).

Usage:
    python src/daily_import.py
    python src/daily_import.py --dry-run
"""

import duckdb
import argparse
import shutil
import hashlib
from pathlib import Path
from datetime import datetime
import sys

# Add parent directory to path to import from same package
sys.path.insert(0, str(Path(__file__).parent))
from import_healthkit import import_csv
from import_medications import import_medications_csv
from import_workouts import import_workouts_csv
from import_cycletracking import import_cycletracking_csv
from validate import run_validation
from config import get_db_path, get_icloud_folder

# Paths from config
DB_PATH = get_db_path()
ICLOUD_FOLDER = get_icloud_folder()

def calculate_file_hash(file_path):
    """
    Calculate SHA-256 hash of a file.
    
    Args:
        file_path: Path to file
    
    Returns:
        str: Hex digest of file hash
    """
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        # Read file in chunks for memory efficiency
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def get_csv_files(folder_path):
    """
    Find all CSV files in the folder.
    
    Returns:
        list: List of Path objects for CSV files
    """
    if not folder_path.exists():
        print(f"❌ Folder not found: {folder_path}")
        return []
    
    csv_files = sorted(folder_path.glob("*.csv"))
    return csv_files

def get_imported_files():
    """
    Get mapping of already-imported files to their hashes from database.
    
    Returns:
        dict: {filename: file_hash} mapping (hash may be None for old imports)
    """
    conn = duckdb.connect(str(DB_PATH))
    try:
        result = conn.execute("SELECT filename, file_hash FROM imports").fetchall()
        return {row[0]: row[1] for row in result}
    finally:
        conn.close()

def run_daily_import(dry_run=False):
    """
    Scan for new CSV files and import them.
    Detects changes via file hash and re-imports updated files.
    
    Args:
        dry_run: If True, only report what would be imported
    
    Returns:
        dict: Summary statistics
    """
    print(f"🔍 Scanning: {ICLOUD_FOLDER}")
    
    # Find all CSVs
    csv_files = get_csv_files(ICLOUD_FOLDER)
    
    if not csv_files:
        print("⚠️  No CSV files found")
        return {"total": 0, "new": 0, "changed": 0, "skipped": 0, "imported": 0, "errors": 0, "rows_added": 0}
    
    print(f"📂 Found {len(csv_files)} CSV file(s)")
    
    # Get already-imported files with their hashes
    imported = get_imported_files()
    
    # Categorize files: new, changed, or unchanged
    new_files = []
    changed_files = []
    unchanged_count = 0
    
    print("🔐 Computing file hashes...")
    for csv_file in csv_files:
        file_hash = calculate_file_hash(csv_file)
        
        if csv_file.name not in imported:
            # Brand new file
            new_files.append((csv_file, file_hash))
        elif imported[csv_file.name] is None:
            # Old import without hash - treat as changed to compute hash
            changed_files.append((csv_file, file_hash, "no_hash"))
        elif imported[csv_file.name] != file_hash:
            # Hash mismatch - file has been updated
            changed_files.append((csv_file, file_hash, "hash_changed"))
        else:
            # Hash matches - file unchanged
            unchanged_count += 1
    
    stats = {
        "total": len(csv_files),
        "new": len(new_files),
        "changed": len(changed_files),
        "skipped": unchanged_count,
        "imported": 0,
        "errors": 0,
        "rows_added": 0
    }
    
    files_to_import = new_files + [(f, h, "new") for f, h in new_files][:0] + changed_files
    
    if not new_files and not changed_files:
        print("✨ No new or changed files to import (all up to date)")
        return stats
    
    if new_files:
        print(f"\n📥 New files to import: {len(new_files)}")
        for f, _ in new_files:
            print(f"   - {f.name}")
    
    if changed_files:
        print(f"\n🔄 Changed files to re-import: {len(changed_files)}")
        for f, _, reason in changed_files:
            if reason == "hash_changed":
                print(f"   - {f.name} (hash changed - file updated)")
            else:
                print(f"   - {f.name} (adding hash to existing import)")
    
    if dry_run:
        print("\n🏃 Dry run mode — no imports performed")
        return stats
    
    # Import new files
    if new_files:
        print("\n⚙️  Importing new files...")
        for csv_file, file_hash in new_files:
            print(f"\n→ {csv_file.name}")
            rows = import_file(csv_file, file_hash, is_reimport=False)
            
            if rows < 0:
                stats["errors"] += 1
            else:
                stats["imported"] += 1
                stats["rows_added"] += rows
    
    # Re-import changed files
    if changed_files:
        print("\n⚙️  Re-importing changed files...")
        for csv_file, file_hash, reason in changed_files:
            print(f"\n🔄 {csv_file.name}")
            if reason == "hash_changed":
                old_hash = imported.get(csv_file.name, "unknown")
                print(f"   Reason: File content changed")
                print(f"   Old hash: {old_hash}")
                print(f"   New hash: {file_hash}")
            rows = import_file(csv_file, file_hash, is_reimport=True)
            
            if rows < 0:
                stats["errors"] += 1
            else:
                stats["imported"] += 1
                stats["rows_added"] += rows
    
    return stats


def import_file(csv_file, file_hash, is_reimport=False):
    """
    Route file to appropriate importer and handle the import.
    
    Args:
        csv_file: Path to CSV file
        file_hash: SHA-256 hash of the file
        is_reimport: True if this is a re-import of an existing file
    
    Returns:
        int: Number of rows added, or -1 on error
    """
    # Route to appropriate importer based on filename
    if csv_file.name.startswith("Medications-"):
        rows = import_medications_csv(csv_file, file_hash, is_reimport)
    elif csv_file.name.startswith("Workouts-"):
        rows = import_workouts_csv(csv_file, file_hash, is_reimport)
    elif csv_file.name.startswith("CycleTracking-"):
        rows = import_cycletracking_csv(csv_file, file_hash, is_reimport)
    elif csv_file.name.startswith("HealthMetrics-"):
        rows = import_csv(csv_file, file_hash, is_reimport)
    elif csv_file.name.startswith("HaishanYe_glucose_"):
        # Skip glucose files - handled separately by import_libre.py
        print(f"⏭️  Skipping glucose file (handled by import_libre.py)")
        return 0
    else:
        print(f"⚠️  Unknown file type, attempting HealthKit import...")
        rows = import_csv(csv_file, file_hash, is_reimport)
    
    return rows

def move_imported_files(dry_run=False):
    """
    Move all imported CSV files (and non-CSV files like JSON/ZIP) to an
    'imported/' subfolder to keep the source folder clean.

    Only moves CSVs that are tracked in the imports table.
    JSON and ZIP files are always moved (not used by the pipeline).

    Args:
        dry_run: If True, only report what would be moved

    Returns:
        int: Number of files moved
    """
    imported_dir = ICLOUD_FOLDER / "imported"
    imported_files_dict = get_imported_files()

    # Collect files to move:
    # 1. CSVs that are in the imports table
    # 2. JSON and ZIP files (not used by pipeline)
    files_to_move = []
    for f in sorted(ICLOUD_FOLDER.iterdir()):
        if f.is_dir():
            continue
        if f.suffix == ".csv" and f.name in imported_files_dict:
            files_to_move.append(f)
        elif f.suffix in (".json", ".zip"):
            files_to_move.append(f)

    if not files_to_move:
        print("📁 No files to move")
        return 0

    print(f"\n📦 Moving {len(files_to_move)} file(s) to imported/")
    for f in files_to_move:
        print(f"   → {f.name}")

    if dry_run:
        print("🏃 Dry run — no files moved")
        return 0

    imported_dir.mkdir(exist_ok=True)
    moved = 0
    for f in files_to_move:
        try:
            shutil.move(str(f), str(imported_dir / f.name))
            moved += 1
        except Exception as e:
            print(f"   ⚠️  Failed to move {f.name}: {e}")

    print(f"✅ Moved {moved} file(s)")
    return moved


def print_summary(stats):
    """Print summary of import run."""
    print("\n" + "="*60)
    print("📊 IMPORT SUMMARY")
    print("="*60)
    print(f"Total CSV files:       {stats['total']}")
    print(f"Already imported:      {stats['skipped']}")
    print(f"New files found:       {stats['new']}")
    print(f"Changed files:         {stats['changed']}")
    print(f"Successfully imported: {stats['imported']}")
    print(f"Errors:                {stats['errors']}")
    if stats['rows_added'] > 0:
        print(f"Total rows added:      {stats['rows_added']}")
    print("="*60)
    
    if stats['imported'] > 0:
        print("✅ Import complete!")
    elif stats['new'] == 0 and stats['changed'] == 0:
        print("✅ All files up to date")
    else:
        print("⚠️  Some imports failed")

def main():
    parser = argparse.ArgumentParser(description="Daily health data import")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be imported without importing")
    args = parser.parse_args()
    
    print("🏥 Health Data Daily Import")
    print(f"⏰ Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # Check database exists
    if not DB_PATH.exists():
        print(f"❌ Database not found: {DB_PATH}")
        print("   Run: python src/init_db.py")
        sys.exit(1)
    
    # Run import
    stats = run_daily_import(dry_run=args.dry_run)
    
    # Print summary
    print_summary(stats)
    
    # Run data quality validation
    if not args.dry_run and (stats['imported'] > 0 or stats['total'] > 0):
        print("\n⚙️  Running data quality checks...")
        validation_report = run_validation(verbose=False)
        if validation_report:
            validation_report.print_report(verbose=False)
    
    # Move imported files to imported/ subfolder
    if stats['errors'] == 0:
        move_imported_files(dry_run=args.dry_run)
    else:
        print("\n⚠️  Skipping file move due to import errors")
    
    # Exit with error code if there were errors
    sys.exit(1 if stats['errors'] > 0 else 0)

if __name__ == "__main__":
    main()
