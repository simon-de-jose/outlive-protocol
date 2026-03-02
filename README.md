# outlive-protocol

Personal health platform. Scripts for HealthKit data import, LibreView glucose sync, health analytics (Attia/Outlive framework), and nutrition logging. Organized as a hub with three sub-skills.

## Overview

```
outlive-protocol/
├── scripts/          # Python scripts
├── data/             # gurus.json, recipes.json, digest-state.json
├── references/       # hash_based_imports.md and other docs
├── shell/            # Bash helpers (process_meal_photos.sh, resize_image.sh)
├── sub-skills/
│   ├── sync-health-data/SKILL.md
│   ├── analyze-health-data/SKILL.md
│   └── log-nutrition/SKILL.md
├── SKILL.md          # Hub routing skill
├── config.yaml       # Paths config (DB, logs, iCloud folder) — user-specific, gitignored
├── config.example.yaml  # Template — copy to config.yaml and customize
├── requirements.txt  # Python deps
└── .env              # USDA API key (git-ignored)
```

## Prerequisites

- macOS with iCloud Drive enabled
- Health Auto Export app configured to export to iCloud
- LibreView account (for CGM sync)
- USDA FoodData Central API key
- `gh` CLI authenticated (for repo push)
- Python 3.x (system or Homebrew)

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/simon-de-jose/outlive-protocol.git
cd outlive-protocol
```

### 2. Create a Python virtual environment

Create a venv anywhere you like and install the dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Verify:
```bash
python3 -c "import duckdb, yaml, requests, pandas; print('OK')"
```

### 3. Configure your paths

```bash
cp config.example.yaml config.yaml
# Edit config.yaml and set your db_path, log_dir, and icloud_folder
```

`config.yaml` supports `~` expansion, so paths like `~/health-data/health.duckdb` work as-is.

### 4. Configure .env

```bash
cp .env.example .env
# Edit .env and add your USDA_API_KEY and LibreView credentials
```

### 5. Register with OpenClaw (extraDirs)

Add the outlive-protocol skills to OpenClaw's skill discovery:

In your OpenClaw config, add to `extraDirs`:
```
sub-skills/
```

Or reference the absolute path to the `sub-skills/` directory in your clone.

### 6. Initialize the database (first time only)

```bash
python3 scripts/init_db.py
```

## Personalization

The following files are user-specific and should be customized after cloning:

| File | What to change |
|------|----------------|
| `config.yaml` | `data.db_path`, `data.log_dir`, `data.reports_dir`, `data.icloud_folder`, `owner` |
| `.env` | `USDA_API_KEY`, LibreView credentials |
| `data/gurus.json` | X handles of longevity experts you follow |
| `sub-skills/analyze-health-data/SKILL.md` | Health targets and baselines (personal) |

`config.yaml` and `.env` are git-ignored so they won't be committed.

## Cron Definitions

These crons should be configured in OpenClaw:

| Cron Name | Schedule | Command |
|-----------|----------|---------|
| daily-health-import | daily 6 AM PT | `python3 scripts/daily_import.py` |
| libre-glucose-sync | 9,12,15,18,21,0,3 | `python3 scripts/sync_libre.py --graph` |
| outlive-weekly | Sun 8 PM PT | Read `sub-skills/analyze-health-data/SKILL.md` → weekly review |
| outlive-monthly | 1st of month 8 PM PT | Read `sub-skills/analyze-health-data/SKILL.md` → monthly review |
| outlive-digest | Daily | Read `sub-skills/analyze-health-data/SKILL.md` → longevity digest |
| nutrition-daily-checkin | Daily | Read `sub-skills/log-nutrition/SKILL.md` → daily summary |

See `crons.example.md` for full cron payload examples.

## Config Reference

All config is in `config.yaml`:

| Key | Description |
|-----|-------------|
| `owner` | Person's name (used in reports) |
| `display.units` | `metric` or `imperial` |
| `data.db_path` | Path to DuckDB file (supports `~`) |
| `data.log_dir` | Directory for import/validation logs (supports `~`) |
| `data.icloud_folder` | iCloud Health Auto Export folder (supports `~`) |
| `data.reports_dir` | Directory for weekly/monthly health reports (supports `~`) |

Scripts resolve config via `scripts/config.py`. Never hardcode paths — always use `get_db_path()`, `get_log_dir()`, etc.

## Path Assumptions

All data paths (DB, logs, reports, iCloud) are configurable via `config.yaml`. Scripts resolve paths at runtime using `scripts/config.py`, so there are no hardcoded directory assumptions.

Set the `venv` key in `config.yaml` to point at your Python interpreter (or just `python3` if deps are on your PATH). Cron commands should run from the repo root.

## Troubleshooting

**"config.yaml not found"**
- Run scripts from the repo root: `cd <repo-root> && python3 scripts/daily_import.py`
- Or prepend `sys.path.insert(0, '<scripts>') — resolve via shell/paths.sh` before importing config

**"Module not found"**
- Ensure you're using the Python interpreter configured in `config.yaml` (the `venv` key)
- Not the system Python or an unrelated venv

**"DB row count decreased"**
- STOP. Do not proceed. Check the logs in your configured `log_dir`
- DB should be monotonically increasing or stable

**LibreView sync fails**
- API has rate limits; check credentials in `.env`
- Try manually: `cd <repo-root> && python3 scripts/sync_libre.py`

**iCloud import finds 0 new files**
- Check iCloud sync status (Files app or `brctl status`)
- Trigger manual export from Health Auto Export app
