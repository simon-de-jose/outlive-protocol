---
name: sync-health-data
description: Sync health data from HealthKit CSV exports and LibreView CGM. Runs daily via cron. Covers import, sync, log writing, and DB validation.
---

# sync-health-data

Daily health data pipeline: HealthKit CSV import → LibreView glucose sync → DB validation → log.

## Shared Config

- **Python:** `/Users/ye/clawd/.venv/bin/python`
- **Scripts:** `/Users/ye/Projects/outlive-protocol/scripts/`
- **Config:** `/Users/ye/Projects/outlive-protocol/config.yaml`
- **DB:** `/Users/ye/clawd/userdata/health/health.duckdb`
- **Logs:** `/Users/ye/clawd/userdata/health/logs/`
- **iCloud folder:** `/Users/ye/Library/Mobile Documents/com~apple~CloudDocs/Juan Health Data`

## Step 1: HealthKit Import

```bash
cd /Users/ye/Projects/outlive-protocol && \
  /Users/ye/clawd/.venv/bin/python scripts/daily_import.py
```

Captures: new_files count, rows_added, any errors.

The script reads CSV exports from the iCloud folder, hashes them to avoid re-importing, and bulk-inserts into the `readings` table.

## Step 2: LibreView Glucose Sync

```bash
cd /Users/ye/Projects/outlive-protocol && \
  /Users/ye/clawd/.venv/bin/python scripts/sync_libre.py --graph
```

Captures: new_readings count, latest reading (timestamp + mg/dL value).

## Step 3: Validate DB

```bash
/Users/ye/clawd/.venv/bin/python -c "
import sys; sys.path.insert(0, '/Users/ye/Projects/outlive-protocol/scripts')
from config import get_db_path
import duckdb
db = duckdb.connect(str(get_db_path()), read_only=True)
print(db.sql('SELECT source, COUNT(*) FROM readings GROUP BY source').fetchall())
db.close()
"
```

**IMPORTANT:** Always use `config.get_db_path()` — never hardcode the DB path.

## Step 4: Write Log

Path: `/Users/ye/clawd/userdata/health/logs/YYYY-MM-DD-health-import.log`

Include: step results, row counts, any errors.

## Cron Definition (daily-dots-in-life)

The sync steps above are embedded in the `daily-dots-in-life` cron job which also posts a thread to Discord #routine. These paths will be updated in the cron jobs during Stage B.

## Troubleshooting

- **"config.yaml not found"** → Run from `/Users/ye/Projects/outlive-protocol/`, not from scripts/ subdirectory
- **DB path wrong** → Never hardcode; always use `config.get_db_path()`
- **LibreView fails** → Check credentials in `.env`; API has rate limits
- **Import finds 0 new files** → iCloud sync may be behind; check iCloud status
