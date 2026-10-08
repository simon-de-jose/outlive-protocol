# cc-smoke-test: coach-cardio

## 1. Discovery
Ask: "Which skills are available?"
**Pass:** response lists `coach-cardio`.

## 2. Invocation — Zone 2 check
Prompt: "How's my Zone 2 training looking this month? Use coach-cardio."
**Pass:**
- CC invokes `coach-cardio`.
- Classifies workouts by HR zone, reports weekly minutes, flags under-/over-target.
- Pulls from the HealthKit-backed DuckDB, not made-up numbers.

## 3. Invocation — VO2 max trend
Prompt: "Is my VO2 max trending up? Plot or summarize."
**Pass:** skill invoked; response includes a real trend statement grounded in data + the time window used.

## 4. Negative
Prompt: "What's a good Zone 2 heart rate in general?"
**Pass:** CC answers generically, does NOT invoke coach-cardio (no personal data needed).

## Runs
<!-- date | pass/fail | notes — record pass/fail and behavior only; never paste personal health values -->
- 2026-04-18 | PASS (with caveat) | Discovery and scenario 2 passed; result values omitted (personal data). Remaining scenarios not run.
