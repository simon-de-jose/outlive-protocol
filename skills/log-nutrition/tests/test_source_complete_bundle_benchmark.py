from __future__ import annotations
import sys
from pathlib import Path
import pytest
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "skills" / "log-nutrition" / "evals"))
import source_complete_bundle_benchmark as bench  # noqa: E402


def test_source_complete_bundle_benchmark_is_deterministic_network_free_and_bounded():
    result = bench.run_benchmark(samples=5)
    assert result["status"] == "ok" and result["network_calls"] == 0
    assert result["temp_artifacts_unchanged"] is True
    assert set(result["paths"]) == {"legacy_local_bundle", "source_complete_bundle"}
    for metrics in result["paths"].values():
        assert metrics["sample_count"] == 5
        assert metrics["median_ms"] >= 0 and metrics["max_ms"] >= metrics["median_ms"]
        assert metrics["compact_json_bytes"] > 0
    legacy = result["paths"]["legacy_local_bundle"]
    source = result["paths"]["source_complete_bundle"]
    assert result["deltas"]["compact_json_bytes"]["bytes"] == source["compact_json_bytes"] - legacy["compact_json_bytes"]
    assert result["deltas"]["compact_json_bytes"]["percent"] == pytest.approx(
        (source["compact_json_bytes"] - legacy["compact_json_bytes"]) / legacy["compact_json_bytes"] * 100
    )
    assert result["deltas"]["median_runtime_ms"]["ms"] == pytest.approx(source["median_ms"] - legacy["median_ms"])
    assert result["deltas"]["median_runtime_ms"]["percent"] == pytest.approx(
        (source["median_ms"] - legacy["median_ms"]) / legacy["median_ms"] * 100
    )
    assert result["gates"]["source_complete_compact_size"] == {
        "passed": True, "observed_bytes": source["compact_json_bytes"], "max_bytes": 16 * 1024,
    }
    runtime_gate = result["gates"]["median_runtime_regression"]
    assert runtime_gate["passed"] is True
    assert runtime_gate["observed_ms"] <= runtime_gate["max_ms"]
    assert "different work" in result["interpretation"] and "not a performance-win claim" in result["interpretation"]
    assert result["source_complete_coverage"]["missing_classes"] == []


def test_benchmark_status_fails_when_compact_bound_is_violated(monkeypatch):
    monkeypatch.setattr(bench, "SOURCE_COMPLETE_MAX_BYTES", 1)
    result = bench.run_benchmark(samples=5)
    assert result["status"] == "failed"
    assert result["gates"]["source_complete_compact_size"]["passed"] is False


def test_source_complete_bundle_benchmark_rejects_tiny_sample_claims():
    with pytest.raises(ValueError, match="at least 5"):
        bench.run_benchmark(samples=4)
