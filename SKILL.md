---
name: outlive-protocol
description: Hub for Juan's personal health platform. Orchestrates data sync, health analytics (Outlive/Attia framework), and nutrition logging. Routes to sub-skills based on task.
---

# Outlive Protocol — Health Platform Hub

Juan's personal health analytics platform, organized as a set of specialized sub-skills. All scripts live in this repo, all data flows through a single DuckDB at `/Users/ye/clawd/userdata/health/health.duckdb`.

## Sub-Skills

| Skill | Location | Purpose |
|-------|----------|---------|
| sync-health-data | `skills/sync-health-data/SKILL.md` | HealthKit CSV import, LibreView glucose sync, DB validation |
| analyze-health-data | `skills/analyze-health-data/SKILL.md` | Health Q&A, weekly/monthly Outlive reviews, longevity digest |
| log-nutrition | `skills/log-nutrition/SKILL.md` | Meal logging from photos/text, USDA lookups, recipe management |

## Shared Paths

| Resource | Path |
|----------|------|
| Python venv | `/Users/ye/clawd/.venv/bin/python` |
| Scripts | `/Users/ye/Projects/outlive-protocol/scripts/` |
| Config | `/Users/ye/Projects/outlive-protocol/config.yaml` |
| Data (DB) | `/Users/ye/clawd/userdata/health/health.duckdb` |
| Logs | `/Users/ye/clawd/userdata/health/logs/` |
| Reports | `/Users/ye/clawd/userdata/health/reports/` |
| Data files | `/Users/ye/Projects/outlive-protocol/data/` |
| Shell scripts | `/Users/ye/Projects/outlive-protocol/shell/` |
| .env (USDA key) | `/Users/ye/Projects/outlive-protocol/.env` |

## When to Use Which Sub-Skill

- **"sync health data" / "run health import" / morning cron** → sync-health-data
- **"how was my sleep?" / "weekly review" / "outlive digest"** → analyze-health-data
- **"log breakfast" / "what did I eat?" / "build a recipe"** → log-nutrition
