"""Recorded trace-contract tests for the source-complete fast path."""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "skills" / "log-nutrition" / "scripts"
EVALS_DIR = REPO_ROOT / "skills" / "log-nutrition" / "evals"
FIXTURE = EVALS_DIR / "fixtures" / "source-complete-fast-path.json"
RECORDED_FIXTURE = EVALS_DIR / "fixtures" / "recorded-openclaw-trace.json"
sys.path[:0] = [str(SCRIPTS_DIR), str(EVALS_DIR)]

import log_nutrition_with_summary as lnws  # noqa: E402
from nutrition_ingest import migrate_database  # noqa: E402
from trace_contract import (  # noqa: E402
    FULL_AGENT_WORKFLOW_BOUNDARY, RecordedTraceError, TraceContractError,
    adapt_openclaw_trace, validate_fixture_nutrient_outcomes, validate_trace,
)


def _entry(name: str, time: str, calories: float, event_key: str) -> dict[str, object]:
    return {
        "event_key": event_key, "meal_time": time, "meal_type": "snack", "meal_name": name,
        "food_items": [{"item": name, "calories": calories}], "calories": calories,
        "source": "fixture evidence_ref",
    }


def _writer_event(payload: dict, result: dict) -> dict:
    entries = payload.get("entries") or [payload]
    return {"type": "writer_call", "artifact": {
        "provider": payload.get("provider"), "message_id": payload.get("message_id"),
        "event_keys": [entry.get("event_key", "default") for entry in entries],
        "replayed": result["write_result"]["replayed"], "status": result["status"],
        "commit_status": result["commit_status"],
    }}


def _validate_synthetic(events, contract):
    """Unit-test ordering with labels; production-adjacent tests use the adapter."""
    return validate_trace(events, {**contract, "synthetic_test": True})


def _dinner_fixture() -> dict:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return next(case for case in fixture["trajectories"] if case["id"] == "dinner-photo-known-exact-id")


def _current_evidence_row(calories: float) -> dict:
    absolute_drift = abs(calories - 789.0)
    return {
        "event_key": "default",
        "calories": calories,
        "explicit_current_evidence": {
            "event_key": "default",
            "evidence": [{
                "scope": "current_meal",
                "kind": "measurement",
                "reference": "current dinner measurement",
            }],
            "justification": {
                "reason_code": "explicit_current_evidence_supersedes_fixture_baseline",
                "metric": "calories",
                "baseline_value": 789.0,
                "observed_value": calories,
            },
            "disclosure": {
                "user_facing": True,
                "metric": "calories",
                "absolute_drift": absolute_drift,
                "message": f"Current-meal evidence changes dinner by {absolute_drift:.1f} kcal.",
            },
        },
    }


def test_frozen_food_journal_fixture_has_exact_identity_nutrients_and_source_bases():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["schema_version"] == 3
    privacy = fixture["source"]["privacy"]
    assert privacy == {
        "attachments_removed": True, "author_identifiers_removed": True,
        "discord_ids_synthetic": True, "entry_ids_synthetic": True,
        "meal_text_synthetic": True, "anonymization_claim": "synthetic",
    }
    by_id = {case["id"]: case for case in fixture["trajectories"]}
    assert {case["parent_message_id"] for case in fixture["trajectories"]} == {
        "1000000000000000001", "1000000000000000002", "1000000000000000004",
        "1000000000000000005", "1000000000000000006",
    }
    assert {case_id: (case["identity"]["message_id"], case["identity"]["event_keys"]) if case["identity"] else None for case_id, case in by_id.items()} == {
        "breakfast-stable-text": ("1000000000000000001", ["default"]),
        "lunch-and-explicit-remember": ("1000000000000000002", ["lunch", "afternoon-snack"]),
        "dinner-photo-known-exact-id": ("1000000000000000004", ["default"]),
        "mozzarella-follow-up-no-write": None,
        "snack-batch-literal-parent": ("1000000000000000006", ["snack-1600-pistachios", "snack-1800-peach", "snack-2030-watermelon"]),
    }
    assert [(row["historical_expected_entry_id"], row["event_key"]) for case in fixture["trajectories"] for row in case["expected_rows"]] == [
        (101, "default"), (102, "lunch"), (103, "afternoon-snack"), (104, "default"),
        (105, "snack-1600-pistachios"), (106, "snack-1800-peach"), (107, "snack-2030-watermelon"),
    ]
    nutrient_fields = {
        "calories", "protein_g", "carbs_g", "fat_total_g", "fat_saturated_g", "fat_unsaturated_g",
        "fat_trans_g", "fiber_g", "sugar_g", "sodium_mg", "potassium_mg", "calcium_mg", "iron_mg",
        "magnesium_mg", "vitamin_d_mcg", "vitamin_b12_mcg", "vitamin_c_mg", "cholesterol_mg",
    }
    for case in fixture["trajectories"]:
        assert case["expected_entry_ids_are_write_inputs"] is False
        assert case["expected_evidence_reasons"]
        assert case["writer_calls"] == (0 if case["id"] == "mozzarella-follow-up-no-write" else 1)
        if case["identity"]:
            assert case["identity"]["message_id"] == case["parent_message_id"]
        for row in case["expected_rows"]:
            assert set(row["nutrients"]) == nutrient_fields
            assert all(row["nutrients"][field] is not None for field in {"calories", "protein_g", "carbs_g", "fat_total_g", "fat_saturated_g", "fiber_g"})
            assert row["source_basis"] and all(isinstance(value, str) for value in row["source_basis"])


def test_dinner_evidence_enrichment_guard_is_absolute_and_preserves_bad_history_diagnosis():
    dinner = _dinner_fixture()
    guard = dinner["evidence_enrichment_guard"]
    known_bad = guard["known_bad_observation"]

    assert guard["max_absolute_delta"] == 50.0
    assert guard["exception_policy"] == {
        "kind": "explicit_current_evidence",
        "required_evidence_scope": "current_meal",
        "allowed_evidence_kinds": ["user_statement", "attachment_label", "measurement"],
        "required_justification_reason_code": "explicit_current_evidence_supersedes_fixture_baseline",
        "requires_user_facing_disclosure": True,
        "historical_evidence_is_not_exception": True,
    }
    assert known_bad["calories"] - dinner["expected_rows"][0]["nutrients"]["calories"] == pytest.approx(127.6)
    assert known_bad["absolute_delta"] == pytest.approx(127.6)
    components = {row["component"]: row for row in known_bad["component_comparison"]}
    assert components["risotto fat and aromatics"] == {
        "component": "risotto fat and aromatics", "fixture_calories": 100.0, "enriched_calories": 205.0,
    }
    assert "entry 40" in known_bad["diagnosis"] and "entry 95" in known_bad["diagnosis"]

    for calories in (789.0, 839.0, 739.0):
        result = validate_fixture_nutrient_outcomes(dinner, [{"event_key": "default", "calories": calories}])
        assert result["status"] == "ok"
        assert result["comparisons"][0]["absolute_delta"] == pytest.approx(abs(calories - 789.0))
        assert result["comparisons"][0]["explicit_current_evidence_exception"] is False

    for calories in (840.0, 738.0, known_bad["calories"]):
        with pytest.raises(TraceContractError, match="evidence_enrichment_nutrient_drift") as exc_info:
            validate_fixture_nutrient_outcomes(dinner, [{"event_key": "default", "calories": calories}])
        assert exc_info.value.violations[0].message == (
            f"default calories absolute drift {abs(calories - 789.0):.1f} exceeds the "
            "50.0 limit without qualifying explicit current evidence"
        )


def test_dinner_drift_exception_requires_structured_current_evidence_and_disclosure():
    dinner = _dinner_fixture()
    for calories in (840.0, 738.0):
        result = validate_fixture_nutrient_outcomes(dinner, [_current_evidence_row(calories)])
        assert result["status"] == "ok"
        assert result["comparisons"][0]["explicit_current_evidence_exception"] is True

    qualifying = _current_evidence_row(840.0)
    free_form_only = {
        "event_key": "default",
        "calories": 840.0,
        "explicit_current_evidence": {"event_key": "default", "rationale": "newer evidence is better"},
    }
    missing_disclosure = deepcopy(qualifying)
    del missing_disclosure["explicit_current_evidence"]["disclosure"]
    historical_recipe = deepcopy(qualifying)
    historical_recipe["explicit_current_evidence"]["evidence"] = [{
        "scope": "historical_recipe",
        "kind": "measurement",
        "reference": "entry 40 salmon risotto",
    }]
    for observed in (free_form_only, missing_disclosure, historical_recipe):
        with pytest.raises(TraceContractError, match="evidence_enrichment_nutrient_drift"):
            validate_fixture_nutrient_outcomes(dinner, [observed])


def test_recorded_ordering_contracts_cover_stable_photo_exact_and_network():
    stable = [{"type": "evidence_packet"}, {"type": "writer_call", "artifact": {}}]
    assert _validate_synthetic(stable, {})["writer_calls"] == 1
    _validate_synthetic([{"type": "image_inspection"}, *stable], {"input_kind": "photo"})
    _validate_synthetic([{"type": "image_inspection"}, {"type": "clarification"}, *stable], {"input_kind": "photo", "ambiguous": True})
    _validate_synthetic(stable, {"exact_id": "known"})
    _validate_synthetic([{"type": "evidence_packet"}, {"type": "targeted_exact_entry_packet"}, {"type": "writer_call", "artifact": {}}], {"exact_id": "discovered"})
    _validate_synthetic([{"type": "evidence_packet"}, {"type": "published_source"}, {"type": "writer_call", "artifact": {}}], {"network_required": True})
    with pytest.raises(TraceContractError, match="clarification_order"):
        _validate_synthetic([{"type": "image_inspection"}, {"type": "evidence_packet"}, {"type": "clarification"}, {"type": "writer_call", "artifact": {}}], {"input_kind": "photo", "ambiguous": True})
    with pytest.raises(TraceContractError, match="unexpected_targeted_packet"):
        _validate_synthetic([{"type": "evidence_packet"}, {"type": "targeted_exact_entry_packet"}, {"type": "writer_call", "artifact": {}}], {"exact_id": "known"})


def test_actual_writer_then_failed_remember_has_no_writer_retry(monkeypatch, tmp_path):
    db = tmp_path / "remember.duckdb"; migrate_database(db)
    payload = {"provider": "discord", "message_id": "1000000000000000002", **_entry("lunch", "2026-01-15T12:00:00", 539, "lunch")}
    writes = 0; original = lnws.ingest_nutrition
    def counted(*args, **kwargs):
        nonlocal writes
        writes += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(lnws, "ingest_nutrition", counted)
    result = lnws.log_nutrition_with_summary(payload, db=db)
    trace = [{"type": "evidence_packet"}, _writer_event(payload, result), {"type": "persistence_failure", "artifact": {"error": "fixture failure"}}]
    _validate_synthetic(trace, {"remember": True, "provider": "discord", "message_id": payload["message_id"], "event_keys": ["lunch"]})
    assert writes == 1 and result["commit_status"] == "committed"
    with pytest.raises(TraceContractError, match="writer_retry"):
        _validate_synthetic([*trace, _writer_event(payload, result)], {"remember": True})


def test_strict_phase_chain_rejects_every_reversed_post_evidence_order():
    writer = {"type": "writer_call", "artifact": {}}
    cases = [
        ([writer, {"type": "evidence_packet"}], {}, "evidence_after_writer"),
        ([{"type": "evidence_packet"}, writer, {"type": "targeted_exact_entry_packet"}], {"exact_id": "discovered"}, "targeted_order"),
        ([{"type": "evidence_packet"}, writer, {"type": "published_source"}], {"network_required": True}, "network_order"),
        ([{"type": "evidence_packet"}, {"type": "persistence"}, writer], {"remember": True}, "persistence_order"),
        ([{"type": "evidence_packet"}, writer, writer], {}, "writer_retry"),
    ]
    for trace, contract, violation in cases:
        with pytest.raises(TraceContractError, match=violation):
            _validate_synthetic(trace, contract)


def test_sanitized_recorded_openclaw_artifacts_normalize_to_contract_events():
    assert FULL_AGENT_WORKFLOW_BOUNDARY == "not implemented before Step 5"
    recording = json.loads(RECORDED_FIXTURE.read_text(encoding="utf-8"))
    trace = adapt_openclaw_trace(recording)
    assert [event["type"] for event in trace] == [
        "image_inspection", "clarification", "evidence_packet", "targeted_exact_entry_packet",
        "published_source", "writer_call", "persistence",
    ]
    result = validate_trace(trace, {
        "input_kind": "photo", "ambiguous": True, "exact_id": "discovered",
        "network_required": True, "remember": True, "provider": "discord",
        "message_id": "1000000000000000002", "event_keys": ["lunch"],
    })
    assert result == {
        "status": "ok", "event_count": 7, "writer_calls": 1,
        "base_packets": 1, "targeted_packets": 1, "network_calls": 1,
    }


def test_recorded_adapter_rejects_overlapping_call_result_intervals():
    contract = {
        "input_kind": "photo", "ambiguous": True, "exact_id": "discovered",
        "network_required": True, "remember": True, "provider": "discord",
        "message_id": "1000000000000000002", "event_keys": ["lunch"],
    }
    original = json.loads(RECORDED_FIXTURE.read_text(encoding="utf-8"))

    def move_after(recording, predicate, anchor):
        records = recording["records"]
        moved = next(record for record in records if predicate(record))
        records.remove(moved)
        anchor_index = next(index for index, record in enumerate(records) if anchor(record))
        records.insert(anchor_index + 1, moved)

    cases = [
        (
            lambda row: row.get("kind") == "tool_result" and row.get("call_id") == "call-image-1",
            lambda row: row.get("kind") == "tool_call" and row.get("call_id") == "call-bundle-1",
            "image_order",
        ),
        (
            lambda row: row.get("kind") == "tool_result" and row.get("call_id") == "call-image-1",
            lambda row: row.get("kind") == "assistant_message" and row.get("message_id") == "assistant-clarify-1",
            "clarification_order",
        ),
        (
            lambda row: row.get("kind") == "user_message" and row.get("message_id") == "user-reply-1",
            lambda row: row.get("kind") == "tool_call" and row.get("call_id") == "call-bundle-1",
            "clarification_order",
        ),
        (
            lambda row: row.get("kind") == "tool_result" and row.get("call_id") == "call-bundle-1",
            lambda row: row.get("kind") == "tool_call" and row.get("call_id") == "call-target-1",
            "targeted_order",
        ),
        (
            lambda row: row.get("kind") == "tool_result" and row.get("call_id") == "call-bundle-1",
            lambda row: row.get("kind") == "tool_call" and row.get("call_id") == "call-source-1",
            "network_order",
        ),
        (
            lambda row: row.get("kind") == "tool_result" and row.get("call_id") == "call-target-1",
            lambda row: row.get("kind") == "tool_call" and row.get("call_id") == "call-writer-1",
            "targeted_order",
        ),
        (
            lambda row: row.get("kind") == "tool_result" and row.get("call_id") == "call-source-1",
            lambda row: row.get("kind") == "tool_call" and row.get("call_id") == "call-writer-1",
            "network_order",
        ),
        (
            lambda row: row.get("kind") == "tool_result" and row.get("call_id") == "call-writer-1",
            lambda row: row.get("kind") == "tool_call" and row.get("call_id") == "call-persist-1",
            "persistence_order",
        ),
    ]
    for moved, anchor, code in cases:
        recording = json.loads(json.dumps(original))
        move_after(recording, moved, anchor)
        with pytest.raises(TraceContractError) as exc_info:
            validate_trace(adapt_openclaw_trace(recording), contract)
        assert code in {violation.code for violation in exc_info.value.violations}


def test_recorded_adapter_rejects_labels_without_call_result_proof_and_bad_cardinality():
    with pytest.raises(TraceContractError, match="unadapted_event"):
        validate_trace([{"type": "evidence_packet"}, {"type": "writer_call"}], {})
    with pytest.raises(RecordedTraceError, match="unsupported recorded artifact kind"):
        adapt_openclaw_trace({"schema_version": 1, "records": [{"kind": "phase", "type": "writer_call"}]})
    recording = json.loads(RECORDED_FIXTURE.read_text(encoding="utf-8"))
    recording["records"] = [record for record in recording["records"] if record.get("call_id") != "call-writer-1" or record["kind"] != "tool_result"]
    with pytest.raises(RecordedTraceError, match="exactly one matching"):
        adapt_openclaw_trace(recording)


def test_actual_batch_literal_parent_event_keys_replay_and_followup_zero_write(tmp_path):
    db = tmp_path / "batch.duckdb"; migrate_database(db)
    event_keys = ["snack-1600-pistachios", "snack-1800-peach", "snack-2030-watermelon"]
    payload = {"provider": "discord", "message_id": "1000000000000000006", "entries": [
        _entry("pistachios", "2026-01-15T16:00:00", 160, event_keys[0]),
        _entry("peach", "2026-01-15T18:00:00", 35, event_keys[1]),
        _entry("watermelon", "2026-01-15T20:30:00", 120, event_keys[2]),
    ]}
    first = lnws.log_nutrition_with_summary(payload, db=db)
    replay = lnws.log_nutrition_with_summary(payload, db=db)
    contract = {"provider": "discord", "message_id": payload["message_id"], "event_keys": event_keys}
    _validate_synthetic([{"type": "evidence_packet"}, _writer_event(payload, first)], contract)
    _validate_synthetic([{"type": "evidence_packet"}, _writer_event(payload, replay)], {**contract, "replay": True})
    _validate_synthetic([{"type": "evidence_packet"}, {"type": "followup_response"}], {"follow_up": True})
    conn = duckdb.connect(str(db), read_only=True)
    try:
        assert conn.execute("SELECT ingest_message_id, ingest_event_key FROM nutrition_log ORDER BY meal_time").fetchall() == [(payload["message_id"], key) for key in event_keys]
    finally:
        conn.close()


def test_actual_summary_failure_and_generic_identity_artifacts(monkeypatch, tmp_path):
    db = tmp_path / "generic.duckdb"; migrate_database(db)
    payload = {"provider": "csv-import", "message_id": "archive:2026/08/22,row=7.part-a", **_entry("imported", "2026-01-15T09:00:00", 42, "segment.1:breakfast")}
    monkeypatch.setattr(lnws, "daily_nutrition_summary", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    first = lnws.log_nutrition_with_summary(payload, db=db)
    _validate_synthetic([{"type": "evidence_packet"}, _writer_event(payload, first)], {
        "provider": "csv-import", "message_id": payload["message_id"], "event_keys": ["segment.1:breakfast"], "summary_failure": True,
    })
    conn = duckdb.connect(str(db), read_only=True)
    try:
        assert conn.execute("SELECT ingest_provider, ingest_message_id, ingest_event_key FROM nutrition_log").fetchone() == ("csv-import", payload["message_id"], "segment.1:breakfast")
    finally:
        conn.close()
