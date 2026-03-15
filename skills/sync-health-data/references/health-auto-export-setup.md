# Health Auto Export — iOS Setup Guide

[Health Auto Export](https://apps.apple.com/us/app/health-auto-export-json-csv/id1115567069) reads your Apple Health data and exports it as CSV files that the import pipeline can process.

## Install & Configure

1. **Install** [Health Auto Export](https://apps.apple.com/us/app/health-auto-export-json-csv/id1115567069) from the App Store (free tier works; Premium enables automations)

2. **Grant Health access** — on first launch, allow access to all health data categories

3. **Configure export settings:**

   Open the app → **Automations** tab → create a new automation (or use manual export):

   | Setting | Value | Why |
   |---------|-------|-----|
   | **Format** | CSV | The import pipeline only reads CSV |
   | **Style** | Table (wide) | One row per timestamp, metrics as columns |
   | **Aggregation** | None / Individual Samples | Raw data, not rolled up |
   | **Export destination** | iCloud Drive folder **or** Shortcuts (for Tailscale) | See delivery routes below |

4. **Select data types to export:**

   The pipeline supports these export categories — configure a separate automation for each:

   | Export Name | File Prefix Expected | What's Captured |
   |------------|---------------------|-----------------|
   | **Health Metrics** | `HealthMetrics-*.csv` | Heart rate, HRV, SpO2, respiratory rate, weight, body fat, sleep stages, wrist temperature, etc. |
   | **Workouts** | `Workouts-*.csv` | Workout type, duration, calories, avg/max heart rate, distance |
   | **Medications** | `Medications-*.csv` | Medication logs from Apple Health |
   | **Cycle Tracking** | `CycleTracking-*.csv` | Cycle tracking data (if applicable) |

   > **Tip:** Start with Health Metrics + Workouts — those cover 90% of the analytics. Add others as needed.

5. **Set the schedule** (Premium feature):
   - **Recommended:** Daily at a consistent time (e.g., 5 AM before the 6 AM import cron)
   - Or use manual export and let the cron pick it up whenever iCloud syncs

## Delivery Routes

You have two options for getting CSVs from your phone to the import pipeline. Both end up in the same folder.

### Option A: iCloud Drive (Simple)

Best for: set-and-forget, no extra infrastructure.

```
iPhone → Health Auto Export → iCloud Drive folder → Mac syncs → daily_import.py
```

**Setup:**
1. In Health Auto Export, set export destination to **iCloud Drive**
2. Create a folder in iCloud Drive (e.g., `Health Data`)
3. Set `HEALTH_ICLOUD_FOLDER` in your `.env` to point to that folder:
   ```
   HEALTH_ICLOUD_FOLDER=~/Library/Mobile Documents/com~apple~CloudDocs/Health Data
   ```
4. The daily cron runs `daily_import.py` which scans this folder

**Pros:** Zero maintenance, works automatically
**Cons:** iCloud sync can lag (minutes to hours); `brctl download` helps but isn't instant

### Option B: Tailscale Upload (Instant)

Best for: real-time imports, no iCloud dependency.

```
iPhone → iOS Shortcut → POST CSV → Tailscale → Upload Server → daily_import.py
```

Requires Tailscale on both phone and computer. See `tailscale-upload.md` for full server setup.

**Setup:**
1. Set up the upload server (see `tailscale-upload.md`)
2. In Health Auto Export, set export destination to **Share Sheet / Shortcuts**
3. Create an iOS Shortcut that POSTs the CSV to your upload server
4. Optionally trigger the Shortcut automatically after each Health Auto Export run

**Pros:** Instant import, works without iCloud
**Cons:** Requires Tailscale setup and upload server running

### Using Both

You can run both routes simultaneously. The import pipeline uses SHA-256 file hashing for deduplication — if the same file arrives via both iCloud and Tailscale, it's only imported once.

## File Naming

Health Auto Export generates filenames automatically. The upload server normalizes them to:

```
HealthMetrics-YYYY-MM-DD.csv
Workouts-YYYY-MM-DD.csv
Medications-YYYY-MM-DD.csv
CycleTracking-YYYY-MM-DD.csv
```

If using iCloud, the app's default filenames work — `daily_import.py` routes files by prefix. Unknown prefixes are attempted as health metrics.

## Verifying It Works

After your first export lands:

```bash
# Check the folder has files
ls $HEALTH_ICLOUD_FOLDER/*.csv

# Dry run to see what would be imported
python3 skills/sync-health-data/scripts/daily_import.py --dry-run

# Actually import
python3 skills/sync-health-data/scripts/daily_import.py

# Verify data in DB
python3 -c "
from bootstrap.env import db_path
import duckdb
db = duckdb.connect(str(db_path()), read_only=True)
print(db.sql('SELECT source, COUNT(*) as rows FROM readings GROUP BY source').fetchdf())
db.close()
"
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No CSV files in folder | Check Health Auto Export ran; check iCloud sync: `brctl status` |
| `daily_import.py` finds 0 new files | Files may be in `imported/` subfolder (already processed) |
| Wrong metrics exported | Verify export style is "Table" (wide format), not "List" |
| Missing sleep/HRV data | Ensure Apple Watch is worn overnight and synced before export |
| Duplicate imports | Not possible — SHA-256 dedup prevents this |
| `Unknown file type` warning | File prefix doesn't match expected names; pipeline tries HealthKit import as fallback |
