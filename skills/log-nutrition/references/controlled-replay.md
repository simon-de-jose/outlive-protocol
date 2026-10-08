## Controlled replay fixture

For the controlled dinner replay fixture, the baseline is `789.0` kcal and `abs(observed - 789.0) <= 50.0` is allowed. Greater drift requires a structured `explicit_current_evidence` record tied to the same literal `event_key`: every evidence item has `scope: current_meal`, allowed kind `user_statement`, `attachment_label`, or `measurement`, and a reference; justification uses reason `explicit_current_evidence_supersedes_fixture_baseline`, metric `calories`, baseline `789.0`, and the exact observed value; user-facing disclosure includes metric, message, and exact absolute drift. History or free-form rationale cannot authorize larger drift.

