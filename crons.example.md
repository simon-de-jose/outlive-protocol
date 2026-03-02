# Cron Setup Examples

> Replace `<repo-root>` with wherever you cloned this repo. All scripts read data paths from `config.yaml`.

> Replace `<repo-root>` with wherever you cloned this repo. All scripts read data paths from `config.yaml`.

These cron definitions show the portable path patterns used in outlive-protocol.
Actual cron configuration is done via OpenClaw's cron manager — this file documents the patterns.

## daily-health-import

```
Schedule: 0 6 * * * (6 AM daily, Pacific Time)

Prompt:
Run the daily health import pipeline:
1. cd <repo-root> && python3 scripts/daily_import.py
2. python3 <repo-root>/scripts/sync_libre.py --graph
3. python3 <repo-root>/scripts/validate.py
Post summary to #outlive-data Discord channel.
```

## libre-glucose-sync

```
Schedule: 0 9,12,15,18,21,0,3 * * * (every 3 hours)

Command: python3 <repo-root>/scripts/sync_libre.py --graph
```

## outlive-weekly

```
Schedule: 0 20 * * 0 (Sunday 8 PM)

Prompt: Read the analyze-health-data skill and generate the weekly Outlive review.
Post to #outlive Discord channel.
```

## outlive-monthly

```
Schedule: 0 20 1 * * (1st of month 8 PM)

Prompt: Read the analyze-health-data skill and generate the monthly Outlive deep dive.
Post to #outlive Discord channel.
```

## outlive-digest

```
Schedule: 0 9 * * * (9 AM daily)

Prompt: Read the analyze-health-data skill and run the longevity digest.
Gurus list: <repo-root>/data/gurus.json
State file: <repo-root>/data/digest-state.json
Post new items to #outlive-digest Discord channel.
```

## nutrition-daily-checkin

```
Schedule: 0 21 * * * (9 PM daily)

Prompt: Read the log-nutrition skill and post today's nutrition summary.
```
