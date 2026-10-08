# Latency-batched workflow evidence

A 2026-08-19 lunch photo flow took 209.4 seconds and 17 tool calls. Individual local tools were fast (mostly 0.1–1.5 seconds); repeated model deliberation between serial calls dominated wall time. The optimized path asks materially necessary photo questions before retrieval, batches independent local evidence needs into one invocation, and combines shared ingest with the post-commit daily summary.

Focused code tests:
- read-only multi-query evidence bundle with no DB mutation;
- partial/fail-safe retrieval behavior;
- write plus coverage-aware summary;
- replay with one nutrition row and one receipt;
- missing saturated-fat coverage remains incomplete;
- post-commit summary failure exposes the authoritative committed write and never attempts a second write.

Parent verification: 4 tests passed in 1.23 seconds on 2026-08-19.