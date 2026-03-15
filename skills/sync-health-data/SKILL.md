---
name: sync-health-data
description: Sync health data from HealthKit CSV exports and LibreView CGM into DuckDB. Runs daily via cron but can also be triggered manually. Use this skill whenever the user asks to import health data, refresh the database, check for new HealthKit exports, sync glucose readings, or troubleshoot missing health data. Also use when data seems stale or out of date.
---

# sync-health-data

Daily health data pipeline: HealthKit CSV import → LibreView glucose sync → DB validation → log.


## Step 0: Force iCloud Sync

```bash
# Resolve iCloud folder from config (never hardcode)
ICLOUD=$(bash ../../shell/paths.sh | grep '^icloud=' | cut -d= -f2-)
brctl download "$ICLOUD"
```

`brctl download` is async (returns immediately) but that's fine — the import runs every 3 hours, so files that aren't ready yet will be picked up on the next run. No need to poll.

## Step 1: HealthKit Import

```bash
# Use paths from: bash ../../shell/paths.sh --json
<venv> <scripts>/daily_import.py
```

Captures: new_files count, rows_added, any errors.

The script reads CSV exports from the iCloud folder, hashes them to avoid re-importing, and bulk-inserts into the `readings` table.

## Step 2: LibreView Glucose Sync

```bash
<venv> <scripts>/sync_libre.py --graph
```

Captures: new_readings count, latest reading (timestamp + mg/dL value).

## Step 3: Validate DB

```bash
<venv> -c "
import sys; sys.path.insert(0, '<scripts>')  # resolve via shell/paths.sh
from config import get_db_path
import duckdb
db = duckdb.connect(str(get_db_path()), read_only=True)
print(db.sql('SELECT source, COUNT(*) FROM readings GROUP BY source').fetchall())
db.close()
"
```

**IMPORTANT:** Always use `config.get_db_path()` — never hardcode the DB path.

## Step 4: Write Log

Path: Read from `config.yaml → data.log_dir` / `YYYY-MM-DD-health-import.log`

Include: step results, row counts, any errors.

## Troubleshooting

- **"config.yaml not found"** → Run from repo root (resolve via `../../shell/paths.sh`), not from scripts/ subdirectory
- **DB path wrong** → Never hardcode; always use `config.get_db_path()`
- **LibreView fails** → Check credentials in `.env`; API has rate limits
- **Import finds 0 new files** → iCloud sync may be behind; check iCloud status
