# Cron Setup Examples

> All scripts read data paths from `config.yaml`. Use `~/clawd/skills/outlive-protocol/` as the canonical path.
> Actual cron configuration is done via OpenClaw's cron manager — this file documents the patterns.

## daily-health-import

```
Schedule: 0 6 * * * (6 AM daily, Pacific Time)
Posts to: #system

Steps:
1. ~/clawd/.venv/bin/python ~/clawd/skills/outlive-protocol/scripts/daily_import.py
2. ~/clawd/.venv/bin/python ~/clawd/skills/outlive-protocol/scripts/sync_libre.py --graph
3. Verify DB row counts
4. Post summary to #system
```

## libre-glucose-sync

```
Schedule: 0 9,12,15,18,21,0,3 * * * (every 3 hours)

Command: ~/clawd/.venv/bin/python ~/clawd/skills/outlive-protocol/scripts/sync_libre.py --graph
```

## outlive-weekly

```
Schedule: 0 20 * * 0 (Sunday 8 PM)
Posts to: #outlive

Read the analyze-health-data skill and generate the weekly Outlive review.
```

## outlive-monthly

```
Schedule: 0 20 1 * * (1st of month 8 PM)
Posts to: #outlive

Read the analyze-health-data skill and generate the monthly Outlive deep dive.
```

## outlive-digest

```
Schedule: 0 19 * * * (7 PM daily)
Posts to: #digest

Read gurus from ~/clawd/skills/outlive-protocol/data/gurus.json
State file: ~/clawd/skills/outlive-protocol/data/digest-state.json
```

## nutrition-daily-checkin

```
Schedule: 0 7 * * * (7 AM daily)
Posts to: #routine

Creates daily nutrition logging thread.
```
