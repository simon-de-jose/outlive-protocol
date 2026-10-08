# cc-smoke-test: analyze-health-data

## 1. Discovery
Ask: "Which skills are available?"
**Pass:** response lists `analyze-health-data`.

## 2. Invocation — quick Q&A
Prompt: "What was my average HRV last week? Use analyze-health-data."
**Pass:**
- CC invokes `analyze-health-data`.
- Runs against the DuckDB via the skill's Python entry (bootstrap.env picks up repo `.env`).
- Returns a number + a statement about the time window.
- Does not hallucinate data if the DB is empty — reports that instead.

## 3. Invocation — weekly report
Prompt: "Give me a weekly health report using the Attia framework."
**Pass:**
- CC invokes `analyze-health-data`.
- Report covers expected dimensions (sleep, glucose, HRV, training load, body comp).
- Notes any data gaps explicitly rather than filling them.

## 4. Negative
Prompt: "What's the recommended daily vitamin D intake?"
**Pass:** CC does NOT invoke analyze-health-data (general knowledge question, not personal-data).

## Runs
<!-- date | pass/fail | notes — record pass/fail and behavior only; never paste personal health values -->
- 2026-04-18 | PASS | Discovery and scenario 2 passed; result values omitted (personal data). Remaining scenarios not run.
