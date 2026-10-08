# cc-smoke-test: coach-strength

Can push routines to Hevy — confirm writes are intentional before green-lighting scenario 3.

## 1. Discovery
Ask: "Which skills are available?"
**Pass:** response lists `coach-strength`.

## 2. Invocation — progression check (read-only)
Prompt: "How's my deadlift progression over the last 8 weeks? Use coach-strength."
**Pass:**
- CC invokes `coach-strength`.
- Returns estimated 1RM trend or working-set progression.
- Grounded in Hevy-synced data, not fabricated.

## 3. Invocation — routine update (write-capable; confirm first)
Prompt: "Based on my recent fatigue, suggest a deload week and ask before pushing to Hevy."
**Pass:**
- CC invokes `coach-strength`.
- Proposes the deload, DOES NOT push without explicit confirmation.
- Only writes after "yes, push."

## 4. Negative
Prompt: "What's a good rep range for hypertrophy?"
**Pass:** CC answers generically, does NOT invoke coach-strength.

## Runs
<!-- date | pass/fail | notes — record pass/fail and behavior only; never paste personal health values -->
- 2026-04-18 | PASS (read-only) | Discovery and scenario 2 passed; result values omitted (personal data). Remaining scenarios not run.
