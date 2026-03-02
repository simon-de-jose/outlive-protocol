# outlive-protocol

Juan's personal health platform. Scripts for HealthKit data import, LibreView glucose sync, health analytics (Attia/Outlive framework), and nutrition logging. Organized as a hub with three sub-skills.

## Overview

```
outlive-protocol/
├── scripts/          # Python scripts (imported from health-clawkit)
├── data/             # gurus.json, recipes.json, digest-state.json
├── docs/             # HASH_BASED_IMPORTS.md and other references
├── shell/            # Bash helpers (process_meal_photos.sh, resize_image.sh)
├── sub-skills/
│   ├── sync-health-data/SKILL.md
│   ├── analyze-health-data/SKILL.md
│   └── log-nutrition/SKILL.md
├── SKILL.md          # Hub routing skill
├── config.yaml       # Paths config (DB, logs, iCloud folder)
├── requirements.txt  # Python deps
└── .env              # USDA API key (git-ignored)
```

## Prerequisites

- macOS with iCloud Drive enabled
- Health Auto Export app configured to export to iCloud (`Juan Health Data` folder)
- LibreView account (for CGM sync)
- USDA FoodData Central API key
- `gh` CLI authenticated (for repo push)
- Python 3.x (system or Homebrew)

## Setup

### 1. Clone / check out the repo

```bash
cd ~/Projects
git clone https://github.com/simon-de-jose/outlive-protocol.git
```

### 2. Create and populate the clawd-local venv

This venv is shared across all clawd skills that need Python:

```bash
cd ~/clawd
python3 -m venv .venv
.venv/bin/pip install duckdb pyyaml requests pandas
```

Verify:
```bash
/Users/ye/clawd/.venv/bin/python -c "import duckdb, yaml, requests, pandas; print('OK')"
```

### 3. Configure .env

```bash
cp /Users/ye/Projects/outlive-protocol/.env.example /Users/ye/Projects/outlive-protocol/.env
# Edit .env and add your USDA_API_KEY and LibreView credentials
```

### 4. Review config.yaml

```yaml
data:
  db_path: /Users/ye/clawd/userdata/health/health.duckdb
  log_dir: /Users/ye/clawd/userdata/health/logs
  icloud_folder: /Users/ye/Library/Mobile Documents/com~apple~CloudDocs/Juan Health Data
```

Adjust paths if your setup differs.

### 5. Register with OpenClaw (extraDirs)

Add the outlive-protocol skills to OpenClaw's skill discovery:

In your OpenClaw config, add to `extraDirs`:
```
/Users/ye/Projects/outlive-protocol/sub-skills/sync-health-data
/Users/ye/Projects/outlive-protocol/sub-skills/analyze-health-data
/Users/ye/Projects/outlive-protocol/sub-skills/log-nutrition
```

Or register the hub skill as an extraDir and reference sub-skills from it.

### 6. Initialize the database (first time only)

```bash
cd /Users/ye/Projects/outlive-protocol
/Users/ye/clawd/.venv/bin/python scripts/init_db.py
```

## Cron Definitions

These crons should be configured in OpenClaw:

| Cron Name | Schedule | Command |
|-----------|----------|---------|
| daily-dots-in-life | daily 6 AM PT | `cd /Users/ye/Projects/outlive-protocol && /Users/ye/clawd/.venv/bin/python scripts/daily_import.py` |
| libre-glucose-sync | 9,12,15,18,21,0,3 | `cd /Users/ye/Projects/outlive-protocol && /Users/ye/clawd/.venv/bin/python scripts/sync_libre.py --graph` |
| outlive-weekly | Sun 8 PM PT | Read `sub-skills/analyze-health-data/SKILL.md` → weekly review |
| outlive-monthly | 1st of month 8 PM PT | Read `sub-skills/analyze-health-data/SKILL.md` → monthly review |
| outlive-digest | Daily | Read `sub-skills/analyze-health-data/SKILL.md` → longevity digest |
| nutrition-daily-checkin | Daily | Read `sub-skills/log-nutrition/SKILL.md` → daily summary |

> **Note (Stage B):** The existing crons in OpenClaw still reference `health-clawkit`. Update them to point to `outlive-protocol` paths after validating this repo works correctly.

## Config Reference

All config is in `config.yaml`:

| Key | Description |
|-----|-------------|
| `owner` | Person's name (used in reports) |
| `display.units` | `metric` or `imperial` |
| `data.db_path` | Path to DuckDB file |
| `data.log_dir` | Directory for import/validation logs |
| `data.icloud_folder` | iCloud Health Auto Export folder |

Scripts resolve config via `scripts/config.py`. Never hardcode paths — always use `get_db_path()`, `get_log_dir()`, etc.

## Troubleshooting

**"config.yaml not found"**
- Run scripts from the repo root: `cd /Users/ye/Projects/outlive-protocol && /Users/ye/clawd/.venv/bin/python scripts/daily_import.py`
- Or: `sys.path.insert(0, '/Users/ye/Projects/outlive-protocol/scripts')` before importing config

**"Module not found"**
- Ensure you're using the clawd venv: `/Users/ye/clawd/.venv/bin/python`
- Not the system Python or health-clawkit's venv

**"DB row count decreased"**
- STOP. Do not proceed. Check the logs in `/Users/ye/clawd/userdata/health/logs/`
- DB should be monotonically increasing or stable

**LibreView sync fails**
- API has rate limits; check credentials in `.env`
- Try manually: `cd /Users/ye/Projects/outlive-protocol && /Users/ye/clawd/.venv/bin/python scripts/sync_libre.py`

**iCloud import finds 0 new files**
- Check iCloud sync status (Files app or `brctl status`)
- Trigger manual export from Health Auto Export app
