---
name: "log-nutrition"
description: "Log meals with source-complete evidence, exact source identity, one atomic safe write, replay safety, and daily totals."
---

# Log Nutrition

Use for meal logging. Complete routine logs with grounded defaults and disclosed estimates; ask only about unresolved material conflicts. Retrieval supplies candidates and coverage; the agent decides what the user ate. Fuzzy scores alone never establish identity.

## Invariants

- Preserve the original message and attachments. Explicit input overrides defaults, retrieval, inference, and history.
- Use the real literal `provider` and `message_id` for every chat write. Never use `--allow-anonymous`, invent child IDs, or embed labels in `message_id`. Multiple entries reuse the parent ID and use distinct stable `event_key` values.
- Never write with direct SQL, manual IDs, a raw writable DuckDB connection, or re-ingest an already committed event.
- Resolve using current explicit input → current context → specifically referenced history → established household defaults → grounded estimates. Defaults fill gaps; they never override current changes. Silence after an estimate does not confirm it or make it a permanent default.
- Ask only when unresolved alternatives materially change identity, portion, or the meal date and context/history cannot reasonably select one. Collect remaining questions in one round after retrieval. Missing routine oil quantities, clock times, or an omitted familiar portion do not alone require a question.
- Exact reuse requires a concrete prior `entry_id`, selected by the agent from evidence or supplied by the user; no additional user confirmation is required for a resolved same/usual/again claim. Record the selected baseline and apply current changes.
- Prefer current published data for brands/restaurants/packages. If unavailable, use a named compatible generic estimate and disclose it without another permission round. Do not invent an official value or an unsupported portion.
- Record every used `evidence_ref` in `source`. Unknown nutrients remain null, never zero. Published bounds such as `<1g` remain null unless an authorized disclosed estimate within the bound is used.
- A committed or replayed write is authoritative. Summary or remember failure never authorizes another write.

## Procedure

1. **Interpret the complete source.** Identify meal time/type, foods, drinks, quantities, fractions, preparation, sauces/oil, brands, restaurant, recipe/reuse intent, changes, image uncertainty, explicit remember intent, and source identity. Normalize bilingual fraction shorthand by checking the stated total and share arithmetic: for example, half of 1.5 is 0.75 = `3/4`, even when Chinese denominator-first wording is typed as `4/3`; ask only when the arithmetic and context do not resolve it. Use the stated meal date/time. For a dated backfill with no clock time, use a context-compatible approximate meal time and label it estimated; never move a backfill to the message date. Require an offset-bearing finite ISO-8601 timestamp.
2. **Inspect a photo once.** Treat accompanying text as authoritative. Note apparent ambiguities, then check context and defaults before collecting any necessary questions.
3. **Request one source-complete local packet** with `skills/log-nutrition/scripts/nutrition_evidence_bundle.py`. Use one query object per independently resolvable component or reuse claim; batch all known queries, exact entry requests, profile keys, and KB recipe sections. Use atomic bilingual `terms`.
   - Anchor relative claims such as `same morning` or `two days ago` to the referenced event window/date derived from the fixed source timestamp.
   - Context describes the referenced event; omit unknown fields instead of copying the current meal's type.
   - Include `entry_requests` only for IDs already known, scope them to item terms, and request `item`, `portion`, all six key nutrients, and `source`.
   - Set `include_household_defaults:true` on the first packet; it requests the saved home-ingredient basis, coffee and egg defaults. Add relevant allowlisted `nutrition_defaults.*` keys and bounded recipe sections. Missing keys mean missing evidence, not absence of a household habit: look for its canonical recipe or recent established baseline in the same packet/closure.
   - For home cooking, ingredient weights/volumes default to raw/pre-cooking, grains/oats/legumes to dry. Finished bread, plated food and restaurant portions retain their finished-food basis. Resolve routine drinks by context (breakfast coffee versus household latte); retrieve the actual recipe/portion rather than asking again. Do not apply a stale generic bread default over the identified current loaf.
4. **Inspect coverage and compatibility.** `coverage` describes requested local evidence, not correctness. Historical meals and recipes are candidates, not proof of current identity, portion, oil, sauce, yield, or aromatics. Establish matching food, preparation, nutrient basis, current recipe, serving count, and changes using the precedence above, without re-confirming already resolved facts. A user reference to a prior meal establishes intent to reuse; select its concrete baseline. Preserve whether source quantities were measured, defaulted or estimated. Audit calories, protein, carbs, total fat, saturated fat, and fiber.
5. **Close evidence gaps once.** At most one additional local packet may batch all newly discovered material gaps. Narrow unresolved components and use `compact:false` if candidates were truncated. A KB-only exact canonical read may replace this closure but cannot follow it. Do not spend closure solely on trace nutrients when a grounded estimate suffices. Then use a published source, a disclosed grounded estimate, or ask only about remaining material conflicts; never run a third packet or broaden filesystem scope.
6. **Choose the evidence path:**
   - Resolved exact ID reuse: reuse stored nutrients verbatim through `quick_log_text.py` only for an unchanged whole entry; use the wrapper for a selected component or a multi-meal batch.
   - ID-less same/usual/again: locate the referenced date first, select the compatible baseline and log directly; disclose its actual date. If absent, use a well-supported alternative as an explicit assumption, not a false date match. Multiple candidates require a question only when context cannot distinguish materially different versions.
   - Similar/delta: subtract removed per-item nutrients and add evidence for changed/new items.
   - Ingredient reuse: validate identity and raw/cooked basis, then scale only to a current explicit, compatible-default, or confirmed portion.
   - Exact recipe/alias: verify current recipe, serving count, and changes; then construct the meal.
   - Fuzzy recipe: use context/canonical recipe evidence to resolve identity; a similarity score alone is insufficient.
   - Brand/restaurant/package: use published data or a disclosed compatible generic estimate.
   - Unresolved material conflict: ask the consolidated questions and hold only the affected portion/meal. Commit independent resolved meals now; name anything not yet included in totals.
7. **Write each resolved event exactly once.** From `${OUTLIVE_REPO:-$HOME/Projects/outlive-protocol}`, call the shared wrapper for constructed entries:

Use the [constructed/batch and exact-reuse payloads](references/write-examples.md). Keep test paths out of writer JSON; multiple meals use one atomic batch.

Treat `commit_status: committed` as final whether replayed or new. After exact reuse, obtain the daily summary once. If summary is unavailable, report the committed meal, run at most one read-only diagnostic, and never retry the writer.

For a correction to a committed entry, retrieve the exact entry through `nutrition_evidence_bundle.py`, then call `nutrition_correct.py` once with the original `provider`, `message_id`, and `event_key`. Include the original offset-bearing `meal_time` plus every field being changed; cite the correction message in `source` instead of replacing the original ingest identity. Run `daily_nutrition_summary.py` once after the correction.
For partial-day completion, follow [staged completion](references/staged-completion.md) to preserve event identities and avoid duplicating corrections.
8. **Confirm visibly.** Report completed records, per-item portions, calories, key macros, source basis, and any drift disclosure. Briefly disclose new/material assumptions and the selected reuse date; established defaults need only a short label, not a confirmation request or daily disclaimer. Then show coverage-aware daily totals for calories, protein, carbs, total fat, saturated fat, and fiber. If incomplete, say `at least X` and identify what is missing.
9. **Run optional post-commit work separately.** Persist a profile default or recipe only when explicitly asked to remember/save it. Confirm the meal as soon as commit+summary returns; persistence failure does not affect the meal and must be reported separately. Delete processed photos only after commit and visible confirmation. There is no grocery inventory to update (retired 2026-10-04).

Controlled dinner replay: follow [fixture contract](references/controlled-replay.md).

## Normal-run limits

For routine logs, the stable path is one evidence bundle, one decision plus only required exception lookup, one safe writer call, immediate confirmation with summary, then optional remember persistence. Do not run help, schema exploration, test grep, broad repository search, arbitrary recipe sweeps, replay audits, or integrity audits during an ordinary successful log.

Relevant implementation files are under `$OUTLIVE_REPO/skills/log-nutrition/`: `nutrition_evidence_bundle.py`, `log_nutrition_with_summary.py`, `quick_log_text.py`, `daily_nutrition_summary.py`, and the reference files for ingest migration, schema, and recipe format.
