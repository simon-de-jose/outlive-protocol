# Staged completion

For staged completion, assign stable meal/component `event_key` values before the first write. Later newly resolved events use the original literal source message ID and their reserved keys; cite the clarification message in `source`. Never resubmit a changed committed event to the ingest writer: use the correction path above, including when filling an omitted component within that event. Check committed identities first; do not duplicate a component as both correction and new entry. The no-second-writer rule applies to committed events, not previously withheld events.
