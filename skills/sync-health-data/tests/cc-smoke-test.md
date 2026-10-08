# cc-smoke-test: sync-health-data

**Overlap warning:** This skill also runs on an OC cron. Don't invoke from CC while the cron is mid-run. Safest: disable the cron briefly, OR run only when you know the cron has finished for the day.

## 1. Discovery
Ask: "Which skills are available?"
**Pass:** response lists `sync-health-data`.

## 2. Invocation — manual sync
Prompt: "Run a manual health data sync."
**Pass:**
- CC invokes `sync-health-data`.
- HealthKit CSV picked up from the configured path; LibreView pull runs.
- DuckDB updated with new rows; CC reports row counts added per table.
- No errors logged; if there's nothing new, CC says so explicitly.

## 3. Invocation — troubleshoot staleness
Prompt: "Health data looks stale — when was the last sync and what might be wrong?"
**Pass:** skill invoked; reports last-sync timestamp per source; highlights missing/failing sources.

## 4. Negative
Prompt: "Analyze my HRV trend." (analysis, not sync)
**Pass:** CC does NOT invoke sync-health-data (routes to `analyze-health-data` instead).

## Runs
<!-- date | pass/fail | notes -->
