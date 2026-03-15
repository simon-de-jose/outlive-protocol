---
name: sync-health-data
description: Sync health data from HealthKit CSV exports and LibreView CGM into DuckDB. Runs daily via cron but can also be triggered manually. Use this skill whenever the user asks to import health data, refresh the database, check for new HealthKit exports, sync glucose readings, or troubleshoot missing health data. Also use when data seems stale or out of date.
---

> **Path Resolution:** Paths configured via `.env` at repo root. Python scripts use `bootstrap.env` module.

# sync-health-data

Daily health data pipeline: HealthKit CSV import -> LibreView glucose sync -> DB validation -> log.

## Scripts

All scripts are in `scripts/` within this skill directory. Run from repo root:
```bash
python3 skills/sync-health-data/scripts/<script_name>.py
```

## Step 1: Force iCloud Sync

```bash
# HEALTH_ICLOUD_FOLDER is set in .env
brctl download "$HEALTH_ICLOUD_FOLDER"
```

`brctl download` is async (returns immediately) but that's fine -- the import runs every 3 hours, so files that aren't ready yet will be picked up on the next run.

## Step 2: HealthKit Import

```bash
python3 skills/sync-health-data/scripts/daily_import.py
```

Captures: new_files count, rows_added, any errors.

The script reads CSV exports from the iCloud folder, hashes them to avoid re-importing, and bulk-inserts into the `readings` table.

## Step 3: LibreView Glucose Sync

```bash
python3 skills/sync-health-data/scripts/sync_libre.py --graph
```

Captures: new_readings count, latest reading (timestamp + mg/dL value).

## Step 4: Validate DB

```bash
python3 -c "
from bootstrap.env import db_path
import duckdb
db = duckdb.connect(str(db_path()), read_only=True)
print(db.sql('SELECT source, COUNT(*) FROM readings GROUP BY source').fetchall())
db.close()
"
```

**IMPORTANT:** Always use `bootstrap.env.db_path()` -- never hardcode the DB path.

## Step 5: Write Log

Path: `<data_dir>/logs/YYYY-MM-DD-health-import.log`

Include: step results, row counts, any errors.

## References

- `references/health-auto-export-setup.md` -- iOS app setup guide (install, configure, delivery routes)
- `references/hash_based_imports.md` -- SHA-256 change detection design doc
- `references/tailscale-upload.md` -- Upload server setup via Tailscale

## Upload Server

The upload server lives in `server/` within this skill directory. See `references/tailscale-upload.md` for setup.

## Troubleshooting

- **DB path wrong** -> Never hardcode; always use `bootstrap.env.db_path()`
- **LibreView fails** -> Check credentials in `.env`; API has rate limits
- **Import finds 0 new files** -> iCloud sync may be behind; check iCloud status
