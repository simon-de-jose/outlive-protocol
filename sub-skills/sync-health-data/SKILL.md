---
name: sync-health-data
description: Sync health data from HealthKit CSV exports and LibreView CGM. Runs daily via cron. Covers import, sync, log writing, and DB validation.
---

# sync-health-data

Daily health data pipeline: HealthKit CSV import → LibreView glucose sync → DB validation → log.

## Shared Config

- **Python:** `~/clawd/.venv/bin/python`
- **Scripts:** `~/Projects/outlive-protocol/scripts/`
- **Config:** `~/Projects/outlive-protocol/config.yaml`
- **DB:** Read from `config.yaml → data.db_path`
- **Logs:** Read from `config.yaml → data.log_dir`
- **iCloud folder:** Read from `config.yaml → data.icloud_folder`

## Step 1: HealthKit Import

```bash
cd ~/Projects/outlive-protocol && \
  ~/clawd/.venv/bin/python scripts/daily_import.py
```

Captures: new_files count, rows_added, any errors.

The script reads CSV exports from the iCloud folder, hashes them to avoid re-importing, and bulk-inserts into the `readings` table.

## Step 2: LibreView Glucose Sync

```bash
cd ~/Projects/outlive-protocol && \
  ~/clawd/.venv/bin/python scripts/sync_libre.py --graph
```

Captures: new_readings count, latest reading (timestamp + mg/dL value).

## Step 3: Validate DB

```bash
~/clawd/.venv/bin/python -c "
import sys; sys.path.insert(0, '$HOME/Projects/outlive-protocol/scripts')
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

- **"config.yaml not found"** → Run from `~/Projects/outlive-protocol/`, not from scripts/ subdirectory
- **DB path wrong** → Never hardcode; always use `config.get_db_path()`
- **LibreView fails** → Check credentials in `.env`; API has rate limits
- **Import finds 0 new files** → iCloud sync may be behind; check iCloud status
