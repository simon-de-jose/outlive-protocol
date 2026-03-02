---
name: outlive-protocol
description: Hub for Juan's personal health platform. Orchestrates data sync, health analytics (Outlive/Attia framework), and nutrition logging. Routes to sub-skills based on task.
---

# Outlive Protocol — Health Platform Hub

Juan's personal health analytics platform, organized as a set of specialized sub-skills. All scripts live in this repo, all data flows through a single DuckDB (path in `config.yaml → data.db_path`).

## Sub-Skills

| Skill | Location | Purpose |
|-------|----------|---------|
| sync-health-data | `sub-skills/sync-health-data/SKILL.md` | HealthKit CSV import, LibreView glucose sync, DB validation |
| analyze-health-data | `sub-skills/analyze-health-data/SKILL.md` | Health Q&A, weekly/monthly Outlive reviews, longevity digest |
| log-nutrition | `sub-skills/log-nutrition/SKILL.md` | Meal logging from photos/text, USDA lookups, recipe management |

## Shared Paths


> **Path Resolution:** Run `bash <repo_root>/shell/paths.sh --json` to get all resolved paths.
> The repo root is 2 directories up from any sub-skill SKILL.md, or the directory containing this file.
> All examples use `<venv>`, `<scripts>`, `<data>`, etc. — resolve them via `shell/paths.sh` first.

## When to Use Which Sub-Skill

- **"sync health data" / "run health import" / morning cron** → sync-health-data
- **"how was my sleep?" / "weekly review" / "outlive digest"** → analyze-health-data
- **"log breakfast" / "what did I eat?" / "build a recipe"** → log-nutrition
