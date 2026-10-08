#!/usr/bin/env python3
"""Production-adjacent recorded-trace adapter and nutrition contract validator."""
from __future__ import annotations

import json
import math
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Steps 1–4 validate recorded artifacts and contracts only. They do not create
# or claim a full agent workflow; that boundary remains Step 5.
FULL_AGENT_WORKFLOW_BOUNDARY = "not implemented before Step 5"


@dataclass(frozen=True)
class TraceViolation:
    code: str
    message: str


class TraceContractError(AssertionError):
    def __init__(self, violations: list[TraceViolation]):
        self.violations = violations
        super().__init__("; ".join(f"{v.code}: {v.message}" for v in violations))


class RecordedTraceError(ValueError):
    """Raised when recorded OpenClaw artifacts do not satisfy the adapter schema."""


def _finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _qualifying_current_evidence(
    observed_row: dict[str, Any], policy: dict[str, Any], *, event_key: str,
    metric: str, baseline: float, observed: float, absolute_delta: float,
) -> bool:
    """Return whether a drift exception is fully tied to current-meal evidence."""
    record = observed_row.get("explicit_current_evidence")
    if not isinstance(record, dict) or record.get("event_key") != event_key:
        return False

    evidence = record.get("evidence")
    allowed_kinds = set(policy["allowed_evidence_kinds"])
    required_scope = policy["required_evidence_scope"]
    if not isinstance(evidence, list) or not evidence:
        return False
    if any(
        not isinstance(item, dict)
        or item.get("scope") != required_scope
        or item.get("kind") not in allowed_kinds
        or not isinstance(item.get("reference"), str)
        or not item["reference"].strip()
        for item in evidence
    ):
        return False

    justification = record.get("justification")
    if not isinstance(justification, dict):
        return False
    if (
        justification.get("reason_code") != policy["required_justification_reason_code"]
        or justification.get("metric") != metric
        or not _finite_number(justification.get("baseline_value"))
        or not _finite_number(justification.get("observed_value"))
        or abs(float(justification["baseline_value"]) - baseline) > 1e-9
        or abs(float(justification["observed_value"]) - observed) > 1e-9
    ):
        return False

    disclosure = record.get("disclosure")
    return bool(
        isinstance(disclosure, dict)
        and disclosure.get("user_facing") is True
        and disclosure.get("metric") == metric
        and isinstance(disclosure.get("message"), str)
        and disclosure["message"].strip()
        and _finite_number(disclosure.get("absolute_drift"))
        and abs(float(disclosure["absolute_drift"]) - absolute_delta) <= 1e-9
    )


def validate_fixture_nutrient_outcomes(
    case: dict[str, Any], observed_rows: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Apply a fixture's absolute evidence-enrichment outcome guard.

    Evidence packets remain candidate-only. This guard catches a controlled
    replay that uses additional local candidates to silently move a known meal
    beyond its predeclared tolerance. Only structured, explicitly current-meal
    evidence with a matching justification and user-facing disclosure may
    exceed the bound; historical recipe evidence never qualifies.
    """
    guard = case.get("evidence_enrichment_guard")
    if guard is None:
        return {"status": "not_configured", "comparisons": []}
    if not isinstance(guard, dict):
        raise RecordedTraceError("evidence_enrichment_guard must be an object")
    metric = guard.get("metric")
    max_absolute_delta = guard.get("max_absolute_delta")
    exception_policy = guard.get("exception_policy")
    if not isinstance(metric, str) or not metric:
        raise RecordedTraceError("evidence enrichment guard requires a metric")
    if (
        not _finite_number(max_absolute_delta)
        or float(max_absolute_delta) < 0
    ):
        raise RecordedTraceError("evidence enrichment guard requires a finite non-negative max_absolute_delta")
    if not isinstance(exception_policy, dict):
        raise RecordedTraceError("evidence enrichment guard requires an exception_policy object")
    allowed_evidence_kinds = exception_policy.get("allowed_evidence_kinds")
    if (
        exception_policy.get("kind") != "explicit_current_evidence"
        or exception_policy.get("required_evidence_scope") != "current_meal"
        or not isinstance(allowed_evidence_kinds, list)
        or not allowed_evidence_kinds
        or any(not isinstance(value, str) or not value for value in allowed_evidence_kinds)
        or exception_policy.get("required_justification_reason_code")
        != "explicit_current_evidence_supersedes_fixture_baseline"
        or exception_policy.get("requires_user_facing_disclosure") is not True
        or exception_policy.get("historical_evidence_is_not_exception") is not True
    ):
        raise RecordedTraceError("evidence enrichment guard has an invalid explicit-current-evidence exception policy")

    expected_rows = case.get("expected_rows")
    if not isinstance(expected_rows, list) or not expected_rows:
        raise RecordedTraceError("guarded fixture requires expected_rows")
    expected_by_event: dict[str, dict[str, Any]] = {}
    for row in expected_rows:
        if not isinstance(row, dict) or not isinstance(row.get("event_key"), str):
            raise RecordedTraceError("guarded expected rows require string event_key values")
        event_key = row["event_key"]
        if event_key in expected_by_event:
            raise RecordedTraceError("guarded expected event keys must be unique")
        expected_by_event[event_key] = row

    observed_by_event: dict[str, dict[str, Any]] = {}
    for row in observed_rows:
        if not isinstance(row, dict) or not isinstance(row.get("event_key"), str):
            raise RecordedTraceError("guarded observed rows require string event_key values")
        event_key = row["event_key"]
        if event_key in observed_by_event:
            raise RecordedTraceError("guarded observed event keys must be unique")
        observed_by_event[event_key] = row

    violations: list[TraceViolation] = []
    comparisons = []
    for event_key, expected_row in expected_by_event.items():
        observed_row = observed_by_event.get(event_key)
        if observed_row is None:
            violations.append(TraceViolation("outcome_row_missing", f"missing observed event {event_key}"))
            continue
        expected_nutrients = expected_row.get("nutrients")
        expected_value = expected_nutrients.get(metric) if isinstance(expected_nutrients, dict) else None
        observed_value = observed_row.get(metric)
        if not all(_finite_number(value) for value in (expected_value, observed_value)):
            raise RecordedTraceError(f"guarded {metric} values must be finite numbers")
        delta = round(float(observed_value) - float(expected_value), 6)
        absolute_delta = abs(delta)
        exception_applied = absolute_delta > float(max_absolute_delta) + 1e-9 and _qualifying_current_evidence(
            observed_row, exception_policy, event_key=event_key, metric=metric,
            baseline=float(expected_value), observed=float(observed_value), absolute_delta=absolute_delta,
        )
        comparisons.append({
            "event_key": event_key,
            "metric": metric,
            "expected": float(expected_value),
            "observed": float(observed_value),
            "delta": delta,
            "absolute_delta": absolute_delta,
            "max_absolute_delta": float(max_absolute_delta),
            "explicit_current_evidence_exception": exception_applied,
        })
        if absolute_delta > float(max_absolute_delta) + 1e-9 and not exception_applied:
            violations.append(TraceViolation(
                "evidence_enrichment_nutrient_drift",
                f"{event_key} {metric} absolute drift {absolute_delta:.1f} exceeds the "
                f"{float(max_absolute_delta):.1f} limit without qualifying explicit current evidence",
            ))
    unexpected = sorted(set(observed_by_event) - set(expected_by_event))
    if unexpected:
        violations.append(TraceViolation("outcome_row_unexpected", f"unexpected observed event keys: {unexpected}"))
    if violations:
        raise TraceContractError(violations)
    return {"status": "ok", "comparisons": comparisons}


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise RecordedTraceError(f"{label} must be a JSON object or encoded JSON object")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RecordedTraceError(f"{label} is invalid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise RecordedTraceError(f"{label} must decode to an object")
    return decoded


def _exec_invocation(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        raise RecordedTraceError("exec tool call requires a non-empty command")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise RecordedTraceError(f"exec command is not parseable: {exc}") from exc
    scripts = {"nutrition_evidence_bundle.py", "log_nutrition_with_summary.py"}
    matching = [index for index, part in enumerate(argv) if Path(part).name in scripts]
    python_driver = bool(argv) and Path(argv[0]).name in {"python", "python3"}
    if matching != [1] or not python_driver:
        raise RecordedTraceError("exec command must be exactly one Python nutrition contract tool")
    script_index = matching[0]
    if len(argv) != 4 or argv[2] != "--json":
        raise RecordedTraceError("nutrition exec command must contain only script, --json, and one payload")
    payload = _json_object(argv[3], "exec --json payload")
    return Path(argv[script_index]).name, payload


def adapt_openclaw_trace(recording: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate and normalize sanitized OpenClaw call/result artifacts.

    Labels are not evidence. A normalized phase is emitted only after a known
    tool call has exactly one matching result with validated arguments/result.
    """
    if not isinstance(recording, dict) or recording.get("schema_version") != 1:
        raise RecordedTraceError("recording schema_version must equal 1")
    records = recording.get("records")
    if not isinstance(records, list):
        raise RecordedTraceError("recording records must be an array")

    calls: dict[str, tuple[int, dict[str, Any]]] = {}
    results: dict[str, tuple[int, dict[str, Any]]] = {}
    message_records: list[tuple[int, dict[str, Any]]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise RecordedTraceError("every recorded artifact must be an object")
        kind = record.get("kind")
        if kind in {"assistant_message", "user_message"}:
            message_records.append((index, record))
            continue
        if kind not in {"tool_call", "tool_result"}:
            raise RecordedTraceError(f"unsupported recorded artifact kind: {kind!r}")
        call_id = record.get("call_id")
        tool_name = record.get("tool_name")
        if not isinstance(call_id, str) or not call_id.strip():
            raise RecordedTraceError("tool artifacts require a stable non-empty call_id")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise RecordedTraceError("tool artifacts require tool_name")
        target = calls if kind == "tool_call" else results
        if call_id in target:
            raise RecordedTraceError(f"duplicate {kind} for call_id {call_id}")
        target[call_id] = (index, record)
    if set(calls) != set(results):
        raise RecordedTraceError("every tool call must have exactly one matching tool result")

    normalized: list[tuple[int, dict[str, Any]]] = []
    for call_id, (call_index, call) in calls.items():
        result_index, result = results[call_id]
        if result_index <= call_index:
            raise RecordedTraceError(f"tool result precedes call for {call_id}")
        if call["tool_name"] != result["tool_name"]:
            raise RecordedTraceError(f"tool_name mismatch for {call_id}")
        tool_name = call["tool_name"]
        arguments = call.get("arguments")
        result_body = result.get("result")
        if not isinstance(arguments, dict) or not isinstance(result_body, dict):
            raise RecordedTraceError(f"call arguments and result must be objects for {call_id}")

        if tool_name == "image":
            if not any(isinstance(arguments.get(key), (str, list)) and arguments.get(key) for key in ("image", "images")):
                raise RecordedTraceError("image call requires image or images")
            if not isinstance(result_body.get("analysis"), str) or not result_body["analysis"].strip():
                raise RecordedTraceError("image result requires non-empty analysis")
            event = {"type": "image_inspection", "artifact": {"call_id": call_id}}
        elif tool_name == "web_search":
            if not isinstance(arguments.get("query"), str) or not arguments["query"].strip():
                raise RecordedTraceError("web_search call requires query")
            if not isinstance(result_body.get("results"), list) or not result_body["results"]:
                raise RecordedTraceError("web_search result requires at least one published result")
            event = {"type": "published_source", "artifact": {"call_id": call_id}}
        elif tool_name == "exec":
            script, payload = _exec_invocation(arguments)
            if result_body.get("exit_code") != 0:
                raise RecordedTraceError(f"nutrition exec result failed for {call_id}")
            output = _json_object(result_body.get("stdout"), "exec stdout")
            if script == "nutrition_evidence_bundle.py":
                if output.get("read_only") is not True or output.get("network_free") is not True:
                    raise RecordedTraceError("evidence result must prove read_only=true and network_free=true")
                targeted_only = bool(payload.get("entry_requests")) and not any(
                    payload.get(key) for key in ("queries", "profile_keys", "kb_recipe_queries")
                )
                event = {"type": "targeted_exact_entry_packet" if targeted_only else "evidence_packet", "artifact": {"call_id": call_id}}
            else:
                entries = payload.get("entries") or [payload]
                if not isinstance(entries, list) or not entries or any(not isinstance(entry, dict) for entry in entries):
                    raise RecordedTraceError("writer payload entries must be a non-empty object array")
                write_result = output.get("write_result")
                if not isinstance(write_result, dict) or not isinstance(write_result.get("replayed"), bool):
                    raise RecordedTraceError("writer result requires write_result.replayed boolean")
                event = {"type": "writer_call", "artifact": {
                    "call_id": call_id,
                    "provider": payload.get("provider"), "message_id": payload.get("message_id"),
                    "event_keys": [entry.get("event_key", "default") for entry in entries],
                    "replayed": write_result["replayed"], "status": output.get("status"),
                    "commit_status": output.get("commit_status"),
                }}
        elif tool_name in {"edit", "write"}:
            if not isinstance(arguments.get("path"), str) or not arguments["path"].strip():
                raise RecordedTraceError(f"{tool_name} call requires path")
            succeeded = result_body.get("status") == "ok"
            failed = isinstance(result_body.get("error"), str) and bool(result_body["error"].strip())
            if succeeded == failed:
                raise RecordedTraceError("persistence result must contain exactly one of status=ok or error")
            event = {"type": "persistence" if succeeded else "persistence_failure", "artifact": {"call_id": call_id}}
        else:
            raise RecordedTraceError(f"unsupported tool_name: {tool_name}")
        event["_trace_interval"] = {"start_index": call_index, "completion_index": result_index}
        normalized.append((call_index, event))

    assistant_messages: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, record in message_records:
        message_id = record.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise RecordedTraceError("message records require message_id")
        if record["kind"] == "assistant_message":
            if record.get("purpose") == "clarification_request":
                assistant_messages[message_id] = (index, record)
            continue
        reply_to = record.get("reply_to")
        if reply_to in assistant_messages:
            request_index, _ = assistant_messages.pop(reply_to)
            if index <= request_index or not isinstance(record.get("text"), str) or not record["text"].strip():
                raise RecordedTraceError("clarification reply must follow its request and contain text")
            normalized.append((request_index, {
                "type": "clarification",
                "artifact": {"request_message_id": reply_to, "response_message_id": message_id},
                "_trace_interval": {"start_index": request_index, "completion_index": index},
            }))
    if assistant_messages:
        raise RecordedTraceError("clarification request is missing its user reply")
    return [
        {**event, "_adapter_schema": 1}
        for _, event in sorted(normalized, key=lambda item: item[0])
    ]


def validate_trace(events: Iterable[dict[str, Any]], contract: dict[str, Any]) -> dict[str, Any]:
    trace = list(events)
    violations: list[TraceViolation] = []
    if not contract.get("synthetic_test") and any(event.get("_adapter_schema") != 1 for event in trace):
        violations.append(TraceViolation(
            "unadapted_event",
            "production-adjacent validation requires events from adapt_openclaw_trace; arbitrary labels are test-only",
        ))
    names = [str(event.get("type", "")) for event in trace]

    def interval(index: int) -> tuple[int, int]:
        value = trace[index].get("_trace_interval")
        if isinstance(value, dict):
            start = value.get("start_index")
            completion = value.get("completion_index")
            if isinstance(start, int) and isinstance(completion, int) and completion >= start:
                return start, completion
        # Synthetic unit traces have no recorded call/result interval. Preserve
        # their event-order semantics without treating labels as production proof.
        return index, index

    def starts(index: int) -> int:
        return interval(index)[0]

    def completes(index: int) -> int:
        return interval(index)[1]

    def add(code: str, message: str) -> None:
        violations.append(TraceViolation(code, message))

    def positions(name: str) -> list[int]:
        return [index for index, value in enumerate(names) if value == name]

    writers = positions("writer_call")
    packets = positions("evidence_packet")
    targeted = positions("targeted_exact_entry_packet")
    image = positions("image_inspection")
    clarification = positions("clarification")
    network = positions("published_source")
    persistence = positions("persistence") + positions("persistence_failure")

    follow_up = bool(contract.get("follow_up"))
    if follow_up:
        if writers:
            add("follow_up_write", "follow-up traces must not call the writer")
    elif len(writers) != 1:
        add("writer_cardinality", f"expected exactly one writer call, observed {len(writers)}")

    input_kind = contract.get("input_kind", "text")
    ambiguous = bool(contract.get("ambiguous"))
    if input_kind in {"photo", "mixed"}:
        if len(image) != 1:
            add("image_cardinality", f"expected one image inspection, observed {len(image)}")
        if ambiguous and len(clarification) != 1:
            add("clarification_cardinality", "ambiguous visual input requires one clarification")
    elif image:
        add("unexpected_image", "text-only trace may not contain image inspection")

    expected_packets = 0 if follow_up and contract.get("no_evidence_needed") else 1
    if len(packets) != expected_packets:
        add("packet_cardinality", f"expected {expected_packets} base packet(s), observed {len(packets)}")
    exact_id = contract.get("exact_id", "none")
    if exact_id == "discovered":
        if len(targeted) != 1:
            add("targeted_cardinality", "a later-discovered exact ID requires one targeted packet")
    elif targeted:
        add("unexpected_targeted_packet", "targeted packet is allowed only for an ID discovered after the base packet")

    if contract.get("network_required"):
        if len(network) != 1:
            add("network_cardinality", "published-source case requires one network pass-through")
    elif network:
        add("unexpected_network", "local-only case may not contain a network phase")

    if contract.get("remember"):
        if len(persistence) != 1:
            add("persistence_cardinality", "remember trace requires one persistence result")
    elif persistence:
        add("unexpected_persistence", "persistence requires an explicit remember request")

    # One strict phase chain: image/clarification -> base -> targeted/network ->
    # writer -> explicit persistence result. Pairwise checks provide actionable
    # violations while writer cardinality independently rejects retries.
    if image and packets and completes(image[0]) >= starts(packets[0]):
        add("image_order", "original images must be inspected before the base packet")
    if image and clarification and completes(image[0]) >= starts(clarification[0]):
        add("clarification_order", "visual clarification must follow image inspection")
    if clarification and packets and completes(clarification[0]) >= starts(packets[0]):
        add("clarification_order", "clarification must precede the base packet")
    if packets and targeted and completes(packets[0]) >= starts(targeted[0]):
        add("targeted_order", "targeted packet must follow the base packet")
    if packets and network and completes(packets[0]) >= starts(network[0]):
        add("network_order", "published source pass-through must follow the base packet")
    for evidence_index, code, message in [
        *( (index, "evidence_after_writer", "base evidence packet must precede the writer") for index in packets ),
        *( (index, "targeted_order", "targeted packet must precede the writer") for index in targeted ),
        *( (index, "network_order", "published source must precede the writer") for index in network ),
    ]:
        if writers and completes(evidence_index) >= starts(writers[0]):
            add(code, message)
    if persistence and writers and completes(writers[0]) >= starts(persistence[0]):
        add("persistence_order", "persistence must happen after the meal writer")
    if len(writers) > 1:
        add("writer_retry", "the writer must never be retried within one trace")

    expected_provider = contract.get("provider")
    expected_message_id = contract.get("message_id")
    expected_event_keys = contract.get("event_keys")
    for index in writers:
        artifact = trace[index].get("artifact") or {}
        if expected_provider is not None and artifact.get("provider") != expected_provider:
            add("provider_identity", "writer artifact provider differs from contract")
        if expected_message_id is not None and artifact.get("message_id") != expected_message_id:
            add("literal_parent_identity", "writer must retain the literal parent message ID")
        if expected_event_keys is not None and artifact.get("event_keys") != expected_event_keys:
            add("event_keys", "writer event keys differ from the exact expected sequence")
        if contract.get("replay") and artifact.get("replayed") is not True:
            add("replay", "replay writer artifact must report replayed=true")
        if contract.get("summary_failure"):
            if artifact.get("status") != "summary_unavailable" or artifact.get("commit_status") != "committed":
                add("summary_failure", "summary failure must preserve committed meal status")

    if violations:
        raise TraceContractError(violations)
    return {
        "status": "ok", "event_count": len(trace), "writer_calls": len(writers),
        "base_packets": len(packets), "targeted_packets": len(targeted), "network_calls": len(network),
    }
