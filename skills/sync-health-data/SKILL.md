---
name: sync-health-data
description: Sync health data from HealthKit CSV exports and LibreView CGM into DuckDB. Runs daily via cron but can also be triggered manually. Use this skill whenever the user asks to import health data, refresh the database, check for new HealthKit exports, sync glucose readings, or troubleshoot missing health data. Also use when data seems stale or out of date.
---

> **Path Resolution:** Run `bash ../../shell/paths.sh --json` to get `venv` and `data_dir`. Scripts are at `scripts/` relative to this skill directory.

# sync-health-data

Daily health data pipeline: HealthKit CSV import → LibreView glucose sync → DB validation → log.

## Scripts

All scripts are in `scripts/` within this skill directory. Run from repo root:
```bash
<venv> skills/sync-health-data/scripts/<script_name>.py
```

## Step 1: Force iCloud Sync

```bash
ICLOUD=$(bash shell/paths.sh | grep '^icloud=' | cut -d= -f2-)
brctl download "$ICLOUD"
```

`brctl download` is async (returns immediately) but that's fine — the import runs every 3 hours, so files that aren't ready yet will be picked up on the next run.

## Step 2: HealthKit Import

```bash
<venv> skills/sync-health-data/scripts/daily_import.py
```

Captures: new_files count, rows_added, any errors.

The script reads CSV exports from the iCloud folder, hashes them to avoid re-importing, and bulk-inserts into the `readings` table.

## Step 3: LibreView Glucose Sync

```bash
<venv> skills/sync-health-data/scripts/sync_libre.py --graph
```

Captures: new_readings count, latest reading (timestamp + mg/dL value).

## Step 4: Validate DB

```bash
<venv> -c "
import sys; sys.path.insert(0, 'skills/sync-health-data/scripts')
from config import get_db_path
import duckdb
db = duckdb.connect(str(get_db_path()), read_only=True)
print(db.sql('SELECT source, COUNT(*) FROM readings GROUP BY source').fetchall())
db.close()
"
```

**IMPORTANT:** Always use `config.get_db_path()` — never hardcode the DB path.

## Step 5: Write Log

Path: `<data_dir>/logs/YYYY-MM-DD-health-import.log`

Include: step results, row counts, any errors.

## Troubleshooting

- **"config.yaml not found"** → Run from repo root, not from a subdirectory
- **DB path wrong** → Never hardcode; always use `config.get_db_path()`
- **LibreView fails** → Check credentials in `.env`; API has rate limits
- **Import finds 0 new files** → iCloud sync may be behind; check iCloud status
