# outlive-protocol

Personal health platform. Scripts for HealthKit data import, LibreView glucose sync, health analytics (Attia/Outlive framework), and nutrition logging. Organized as a hub with three sub-skills.

## Overview

```
outlive-protocol/
├── scripts/          # Python scripts (config.py, daily_import.py, sync_libre.py, etc.)
├── data/             # Example files only (recipes.example.json, gurus.example.json, etc.)
├── references/       # Technical docs (hash_based_imports.md)
├── shell/            # Bash helpers (paths.sh, process_meal_photos.sh, resize_image.sh)
├── sub-skills/
│   ├── sync-health-data/SKILL.md
│   ├── analyze-health-data/SKILL.md
│   └── log-nutrition/SKILL.md
├── SKILL.md          # Hub routing skill
├── config.yaml       # User config (gitignored — copy from config.example.yaml)
├── config.example.yaml
├── requirements.txt  # Python deps
└── .env              # API keys (gitignored — copy from .env.example)
```

## Quick Start

```bash
git clone https://github.com/simon-de-jose/outlive-protocol.git
cd outlive-protocol

# Create config files
cp config.example.yaml config.yaml
cp .env.example .env

# Edit config.yaml — set your data_dir and icloud_folder
# Edit .env — add your USDA API key and LibreView credentials

# Install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Initialize database
python3 scripts/init_db.py

# Copy example data files to your data_dir
cp data/gurus.example.json <your-data-dir>/gurus.json
cp data/recipes.example.json <your-data-dir>/recipes.json
cp data/digest-state.example.json <your-data-dir>/digest-state.json
cp data/user-profile.example.yaml <your-data-dir>/user-profile.yaml
```

## Prerequisites

- macOS with iCloud Drive enabled
- Health Auto Export app configured to export to iCloud
- LibreView account (for CGM sync)
- USDA FoodData Central API key ([get one here](https://fdc.nal.usda.gov/api-key-signup))
- Python 3.x

## Data Architecture

All user data lives in a single configurable directory (`data_dir`):

```
<data_dir>/                     # e.g. ~/health-data
├── health.duckdb               # Main database
├── logs/                       # Import/validation logs
├── reports/
│   ├── baselines/              # Personal health baselines
│   ├── weekly/                 # Weekly Outlive reports
│   └── monthly/                # Monthly deep dives
├── recipes.json                # Your saved recipes
├── gurus.json                  # Health experts to follow
├── digest-state.json           # Digest dedup state
└── user-profile.yaml           # Libre patient name, nutrition defaults
```

The repo's `data/` folder contains only example/template files. Your actual data stays outside the repo.

## Config Reference

All config is in `config.yaml`:

| Key | Description |
|-----|-------------|
| `owner` | Your name (used in reports) |
| `display.units` | `metric` or `imperial` |
| `venv` | Python interpreter path (or just `python3`) |
| `data.data_dir` | Consolidated data directory — everything goes here |
| `data.icloud_folder` | iCloud Health Auto Export folder |

Optional overrides (if not set, derived from `data_dir`):

| Key | Default |
|-----|---------|
| `data.db_path` | `<data_dir>/health.duckdb` |
| `data.log_dir` | `<data_dir>/logs/` |
| `data.reports_dir` | `<data_dir>/reports/` |

Scripts resolve config via `scripts/config.py`. Never hardcode paths — always use `get_db_path()`, `get_log_dir()`, etc.

## Personalization

After cloning, customize these files:

| File | What to set |
|------|-------------|
| `config.yaml` | `owner`, `data.data_dir`, `data.icloud_folder`, `venv` |
| `.env` | `USDA_API_KEY`, LibreView credentials (in macOS Keychain) |
| `<data_dir>/user-profile.yaml` | `libre_patient_name`, nutrition defaults |
| `<data_dir>/gurus.json` | X handles of longevity experts you follow |

After first week of data, generate a baseline: the analyze-health-data skill will reference it for tracking progress.

## OpenClaw Integration

### Register skills

Add to your OpenClaw config's `skills.load.extraDirs`:
```json
["<path-to-clone>/sub-skills"]
```

### Cron definitions

| Cron Name | Schedule | What |
|-----------|----------|------|
| daily-health-import | daily 6 AM | HealthKit CSV import + Libre sync + validation |
| libre-glucose-sync | every 3 hrs | Libre CGM glucose readings |
| outlive-weekly | Sun 8 PM | Weekly Outlive scorecard |
| outlive-monthly | 1st of month 8 PM | Monthly deep dive |
| outlive-digest | daily | Longevity news from health experts on X |
| nutrition-daily-checkin | daily | Nutrition logging thread |

See `crons.example.md` for full cron payload examples.

## Troubleshooting

**"config.yaml not found"**
- Run scripts from the repo root: `cd <repo-root> && python3 scripts/daily_import.py`

**"Module not found"**
- Ensure you're using the Python interpreter from `config.yaml` (`venv` key)

**"DB row count decreased"**
- STOP. Check logs in `<data_dir>/logs/`. DB should be monotonically increasing.

**LibreView sync fails**
- API has rate limits; check credentials
- Try: `python3 scripts/sync_libre.py --dry-run`

**iCloud import finds 0 new files**
- Check iCloud sync status (`brctl status`)
- Trigger manual export from Health Auto Export app
