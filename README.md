# outlive-protocol

Personal health platform. Scripts for HealthKit data import, LibreView glucose sync, health analytics (Attia/Outlive framework), and nutrition logging. Organized as a collection of OpenClaw skills.

## Overview

```
outlive-protocol/
├── bootstrap/        # Shared foundation (env config, DB init)
├── data/             # Example files only (recipes.example.json, gurus.example.json, etc.)
├── skills/
│   ├── analyze-health-data/
│   │   ├── SKILL.md
│   │   └── references/         # Attia framework, report templates, target ranges
│   ├── coach-cardio/
│   │   ├── SKILL.md
│   │   └── references/         # Queries, benchmarks
│   ├── coach-nutrition/
│   │   ├── SKILL.md
│   │   ├── scripts/            # nutrition_summary.py
│   │   └── references/         # Queries, cross-skill correlations
│   ├── coach-strength/
│   │   ├── SKILL.md
│   │   ├── scripts/            # sync_hevy.py, init_hevy.py
│   │   └── references/         # Hevy API docs
│   ├── log-nutrition/
│   │   ├── SKILL.md
│   │   ├── scripts/            # log_nutrition.py, init_nutrition.py
│   │   └── references/         # DB schema, recipe format
│   └── sync-health-data/
│       ├── SKILL.md
│       ├── scripts/            # daily_import.py, sync_libre.py, validate.py, etc.
│       ├── server/             # Upload server (Bun/TypeScript)
│       └── references/         # Hash imports, Tailscale upload
├── tests/            # Core tests (bootstrap, git hygiene, skill files)
├── pyproject.toml    # Package definition + deps
├── .env              # User config + API keys (gitignored)
└── .env.example      # Template
```

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/simon-de-jose/outlive-protocol.git
cd outlive-protocol
python3 -m venv .venv && source .venv/bin/activate

# 2. Install core (pick what you need)
pip install -e .                       # Core only (analyze, coach-cardio)
pip install -e ".[sync]"               # + health data sync & Libre CGM
pip install -e ".[all]"                # Everything
pip install -e ".[all,dev]"            # Everything + pytest

# 3. Configure
cp .env.example .env
# Edit .env — set HEALTH_DATA_DIR at minimum

# 4. Initialize database
python -m bootstrap.init_db

# 5. Copy example data files to your data dir
cp data/gurus.example.json <your-data-dir>/gurus.json
cp data/recipes.example.json <your-data-dir>/recipes.json
cp data/digest-state.example.json <your-data-dir>/digest-state.json
cp data/user-profile.example.yaml <your-data-dir>/user-profile.yaml

# 6. Run tests
pytest                                 # All tests
pytest skills/coach-cardio/tests/      # Just one skill
```

## Skill Selection Guide

| Skill | What it does | Extra deps | Env vars needed |
|-------|-------------|------------|-----------------|
| `analyze-health-data` | Query & report on health metrics | (core) | -- |
| `coach-cardio` | Zone 2 & VO2 max coaching | (core) | -- |
| `coach-nutrition` | Nutrition analysis & glucose correlation | (core) | -- |
| `coach-strength` | Hevy workout sync & progressive overload | (core) | `HEVY_API_KEY` |
| `log-nutrition` | Log meals from photos/text | (core) | `USDA_API_KEY` |
| `sync-health-data` | Import HealthKit CSVs & Libre CGM | `[sync]` | `HEALTH_ICLOUD_FOLDER` |

## Prerequisites

- macOS with iCloud Drive enabled
- Health Auto Export app ([setup guide](skills/sync-health-data/references/health-auto-export-setup.md))
- LibreView account (for CGM sync)
- USDA FoodData Central API key ([get one here](https://fdc.nal.usda.gov/api-key-signup))
- Hevy Pro account + API key ([settings → developer](https://hevy.com/settings?developer)) — only needed for coach-strength
- Python 3.10+

## Data Architecture

All user data lives in a single configurable directory (`HEALTH_DATA_DIR`):

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

All config is in `.env`:

| Variable | Description |
|----------|-------------|
| `HEALTH_DATA_DIR` | Consolidated data directory (required) |
| `HEALTH_OWNER` | Your name (used in reports) |
| `HEALTH_UNITS` | `metric` or `imperial` |
| `HEALTH_ICLOUD_FOLDER` | iCloud Health Auto Export folder |
| `USDA_API_KEY` | USDA FoodData Central API key |
| `HEVY_API_KEY` | Hevy API key |

Optional overrides (if not set, derived from `HEALTH_DATA_DIR`):

| Variable | Default |
|----------|---------|
| `HEALTH_DB_PATH` | `<data_dir>/health.duckdb` |
| `HEALTH_LOG_DIR` | `<data_dir>/logs/` |
| `HEALTH_REPORTS_DIR` | `<data_dir>/reports/` |

All scripts use `bootstrap.env` to resolve paths. Never hardcode paths.

## Personalization

After cloning, customize these files:

| File | What to set |
|------|-------------|
| `.env` | `HEALTH_DATA_DIR`, `HEALTH_ICLOUD_FOLDER`, API keys |
| `<data_dir>/user-profile.yaml` | `libre_patient_name`, nutrition defaults |
| `<data_dir>/gurus.json` | X handles of longevity experts you follow |

After first week of data, generate a baseline: the analyze-health-data skill will reference it for tracking progress.

## OpenClaw Integration

### Register skills

Add to your OpenClaw config's `skills.load.extraDirs`:
```json
["<path-to-clone>/skills"]
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

**"Module not found"**
- Ensure you've run `pip install -e .` in a venv

**"DB row count decreased"**
- STOP. Check logs in `<data_dir>/logs/`. DB should be monotonically increasing.

**LibreView sync fails**
- API has rate limits; check credentials
- Try: `python3 skills/sync-health-data/scripts/sync_libre.py --dry-run`

**iCloud import finds 0 new files**
- Check iCloud sync status (`brctl status`)
- Trigger manual export from Health Auto Export app
