# Cron Setup Examples

> These cron definitions show the portable patterns used in outlive-protocol.
> Replace `<repo-root>` with your clone path. All scripts read data paths from `config.yaml`.
> Actual cron configuration is done via OpenClaw's cron manager — this file documents the patterns.

## daily-health-import

```
Schedule: 0 6 * * * (6 AM daily, local time)
Posts to: #system

Steps:
1. python3 <repo-root>/scripts/daily_import.py
2. python3 <repo-root>/scripts/sync_libre.py --graph
3. Verify DB row counts
4. Post summary to system channel
```

## libre-glucose-sync

```
Schedule: 0 9,12,15,18,21,0,3 * * * (every 3 hours)

Command: python3 <repo-root>/scripts/sync_libre.py --graph
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

Read gurus from <data_dir>/gurus.json
State file: <data_dir>/digest-state.json
```

## nutrition-daily-checkin

```
Schedule: 0 7 * * * (7 AM daily)
Posts to: #routine

Creates daily nutrition logging thread.
```
