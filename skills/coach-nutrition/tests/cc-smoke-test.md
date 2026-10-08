# cc-smoke-test: coach-nutrition

## 1. Discovery
Ask: "Which skills are available?"
**Pass:** response lists `coach-nutrition`.

## 2. Invocation — protein adequacy
Prompt: "Am I hitting protein targets? Use coach-nutrition."
**Pass:**
- CC invokes `coach-nutrition`.
- Returns g/kg/day with a time window.
- References the protein target (Attia framework default) and the actual intake.

## 3. Invocation — glucose-meal correlation
Prompt: "Which meals last week spiked my glucose the most?"
**Pass:** skill invoked; returns a ranked list tying meal entries to CGM readings. Says so if data is insufficient.

## 4. Negative
Prompt: "What are good sources of omega-3?"
**Pass:** CC answers generically, does NOT invoke coach-nutrition.

## Runs
<!-- date | pass/fail | notes — record pass/fail and behavior only; never paste personal health values -->
- 2026-04-18 | PASS | Discovery and scenario 2 passed; result values omitted (personal data). Remaining scenarios not run.
