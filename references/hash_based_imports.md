# Hash-Based Change Detection for Health Data Imports

## Overview

The outlive-protocol import system now uses SHA-256 file hashing to detect when CSV files have been updated and need re-importing. This solves the problem where re-exported files with additional data were being skipped.

## How It Works

### 1. File Hash Tracking

Every imported file is tracked with:
- **Filename**: e.g., `HealthMetrics-2026-02-08.csv`
- **SHA-256 Hash**: Computed from file contents
- **Import metadata**: timestamp, rows added, source

### 2. Change Detection

When scanning for files to import, the system:

1. **Calculates hash** for each CSV file
2. **Compares** against stored hash in database:
   - **No record** → New file → Import
   - **Hash = NULL** → Old import → Add hash + re-import
   - **Hash differs** → File updated → Re-import
   - **Hash matches** → No change → Skip

### 3. Upsert Logic

Re-imports use `INSERT OR IGNORE` with natural keys:

| Table | Natural Key |
|-------|-------------|
| `readings` | (timestamp, metric, source) |
| `medications` | (timestamp, medication) |
| `workouts` | (start_time, type) |

This ensures:
- No duplicate rows
- Only new data gets added
- Existing data remains unchanged

## Usage

### Running Daily Import

```bash
cd <repo-root>

# Dry run (see what would be imported)
python3 scripts/daily_import.py --dry-run

# Actual import
python3 scripts/daily_import.py
```

### Expected Output

#### First Run (Backfilling Hashes)
```
🔍 Scanning: <your configured icloud_folder>
📂 Found 10 CSV file(s)
🔐 Computing file hashes...

🔄 Changed files to re-import: 10
   - HealthMetrics-2026-02-01.csv (adding hash to existing import)
   - HealthMetrics-2026-02-02.csv (adding hash to existing import)
   ...
```

#### Subsequent Runs (Normal Operation)
```
🔍 Scanning: <your configured icloud_folder>
📂 Found 5 CSV file(s)
🔐 Computing file hashes...

📥 New files to import: 1
   - HealthMetrics-2026-02-10.csv

🔄 Changed files to re-import: 1
   - HealthMetrics-2026-02-05.csv (hash changed - file updated)

✨ No new or changed files to import (all up to date)
```

## Migration

The migration has already been applied. To verify:

```bash
python3 << 'EOF'
import duckdb
from config import get_db_path
conn = duckdb.connect(str(get_db_path()))
columns = [col[1] for col in conn.execute("PRAGMA table_info(imports)").fetchall()]
print("✅ file_hash column exists" if 'file_hash' in columns else "❌ Migration needed")
conn.close()
EOF
```

If migration is needed:
```bash
python3 scripts/migrate_add_file_hash.py
```

## Backwards Compatibility

- ✅ **Existing imports**: Work normally, hash=NULL treated as "needs hash"
- ✅ **Old data**: Unchanged, no duplicates created
- ✅ **INSERT OR IGNORE**: Existing deduplication logic preserved
- ✅ **Cron jobs**: No changes needed

## Testing

### Test Hash-Based Detection

```bash
# 1. Copy a file back from imported/
cd "<your configured icloud_folder>"
cp imported/HealthMetrics-2026-02-08.csv .

# 2. Run dry-run (should skip - hash matches)
cd <repo-root>
python3 scripts/daily_import.py --dry-run
# Expected: "✨ No new or changed files to import (all up to date)"

# 3. Modify file to change hash
echo "Modified" >> HealthMetrics-2026-02-08.csv

# 4. Run dry-run again (should detect change)
python3 scripts/daily_import.py --dry-run
# Expected: "🔄 Changed files to re-import: 1"
#           "   - HealthMetrics-2026-02-08.csv (hash changed - file updated)"

# 5. Clean up
rm HealthMetrics-2026-02-08.csv
```

## Database Schema

### Before
```sql
CREATE TABLE imports (
    import_id INTEGER PRIMARY KEY,
    filename VARCHAR UNIQUE NOT NULL,
    imported_at TIMESTAMP NOT NULL,
    rows_added INTEGER NOT NULL,
    source VARCHAR NOT NULL
);
```

### After
```sql
CREATE TABLE imports (
    import_id INTEGER PRIMARY KEY,
    filename VARCHAR UNIQUE NOT NULL,
    imported_at TIMESTAMP NOT NULL,
    rows_added INTEGER NOT NULL,
    source VARCHAR NOT NULL,
    file_hash VARCHAR  -- SHA-256 hex digest (64 chars)
);
```

## Benefits

1. **Detects updates**: When Health Auto Export re-exports with more data
2. **Prevents duplicates**: INSERT OR IGNORE ensures idempotency
3. **Clear logging**: Distinguishes new files vs. updated files
4. **Automatic backfill**: Old imports get hashes on next run
5. **Efficient**: Only processes truly changed files
6. **Future-proof**: Any content change triggers re-import

## Troubleshooting

### "Some imports failed" but no errors shown

Check if files are being moved to `imported/` before processing completes. The system only moves files after successful import.

### Old imports showing as "changed" every run

This is expected on first run. After one import with `file_hash` populated, they should stabilize.

To check:
```bash
python3 -c "
import duckdb
from config import get_db_path
conn = duckdb.connect(str(get_db_path()))
count = conn.execute('SELECT COUNT(*) FROM imports WHERE file_hash IS NULL').fetchone()[0]
print(f'{count} imports without hash')
conn.close()
"
```

### Force re-import of a file

```bash
# Clear hash for a specific file
python3 -c "
import duckdb
conn = duckdb.connect(str(get_db_path()))  # see scripts/config.py
conn.execute(\"UPDATE imports SET file_hash = NULL WHERE filename = 'HealthMetrics-2026-02-08.csv'\")
conn.close()
"

# Copy file back and run import
# It will be detected as "changed (adding hash to existing import)"
```

## Implementation Details

- **Hash algorithm**: SHA-256 (64-character hex digest)
- **Hash computation**: File read in 4KB chunks for memory efficiency
- **Import flow**: Calculate hash → Compare → Route to importer → Update DB
- **Importer signature**: `import_X_csv(path, file_hash=None, is_reimport=False)`
- **Database updates**: INSERT on first import, UPDATE on re-import

## Related Files

- `scripts/daily_import.py` - Main orchestrator with hash detection
- `src/import_healthkit.py` - HealthKit data importer
- `src/import_medications.py` - Medications importer
- `src/import_workouts.py` - Workouts importer  
- `src/import_cycletracking.py` - Cycle tracking importer
- `scripts/migrate_add_file_hash.py` - Database migration script
- `CHANGELOG.md` - Detailed change log
