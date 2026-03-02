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
cd ~/Projects
git clone https://github.com/simon-de-jose/outlive-protocol.git
```

### 2. Create the clawd-local venv

This venv is shared across all clawd skills that need Python:

```bash
cd ~/clawd
python3 -m venv .venv
.venv/bin/pip install duckdb pyyaml requests pandas
```

Verify:
```bash
~/clawd/.venv/bin/python -c "import duckdb, yaml, requests, pandas; print('OK')"
```

### 3. Configure your paths

```bash
cp ~/Projects/outlive-protocol/config.example.yaml ~/Projects/outlive-protocol/config.yaml
# Edit config.yaml and set your db_path, log_dir, and icloud_folder
```

`config.yaml` supports `~` expansion, so paths like `~/clawd/userdata/health/health.duckdb` work as-is.

### 4. Configure .env

```bash
cp ~/Projects/outlive-protocol/.env.example ~/Projects/outlive-protocol/.env
# Edit .env and add your USDA_API_KEY and LibreView credentials
```

### 5. Register with OpenClaw (extraDirs)

Add the outlive-protocol skills to OpenClaw's skill discovery:

In your OpenClaw config, add to `extraDirs`:
```
~/clawd/skills/outlive-protocol/sub-skills
```

Or reference the absolute path to the sub-skills directory.

### 6. Initialize the database (first time only)

```bash
cd ~/Projects/outlive-protocol
~/clawd/.venv/bin/python scripts/init_db.py
```

## Personalization

The following files are user-specific and should be customized after cloning:

| File | What to change |
|------|----------------|
| `config.yaml` | `data.db_path`, `data.log_dir`, `data.icloud_folder`, `owner` |
| `.env` | `USDA_API_KEY`, LibreView credentials |
| `data/gurus.json` | X handles of longevity experts you follow |
| `sub-skills/analyze-health-data/SKILL.md` | Health targets and baselines (personal) |

`config.yaml` and `.env` are git-ignored so they won't be committed.

## Cron Definitions

These crons should be configured in OpenClaw:

| Cron Name | Schedule | Command |
|-----------|----------|---------|
| daily-health-import | daily 6 AM PT | `~/clawd/.venv/bin/python ~/Projects/outlive-protocol/scripts/daily_import.py` |
| libre-glucose-sync | 9,12,15,18,21,0,3 | `~/clawd/.venv/bin/python ~/Projects/outlive-protocol/scripts/sync_libre.py --graph` |
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

Scripts resolve config via `scripts/config.py`. Never hardcode paths — always use `get_db_path()`, `get_log_dir()`, etc.

## Troubleshooting

**"config.yaml not found"**
- Run scripts from the repo root: `cd ~/Projects/outlive-protocol && ~/clawd/.venv/bin/python scripts/daily_import.py`
- Or prepend `sys.path.insert(0, '$HOME/Projects/outlive-protocol/scripts')` before importing config

**"Module not found"**
- Ensure you're using the clawd venv: `~/clawd/.venv/bin/python`
- Not the system Python or another venv

**"DB row count decreased"**
- STOP. Do not proceed. Check the logs in `~/clawd/userdata/health/logs/`
- DB should be monotonically increasing or stable

**LibreView sync fails**
- API has rate limits; check credentials in `.env`
- Try manually: `cd ~/Projects/outlive-protocol && ~/clawd/.venv/bin/python scripts/sync_libre.py`

**iCloud import finds 0 new files**
- Check iCloud sync status (Files app or `brctl status`)
- Trigger manual export from Health Auto Export app
