"""Tests for the Step 4 deterministic benchmark harness."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BENCH = REPO_ROOT / "skills" / "log-nutrition" / "evals" / "step4_benchmark.py"
sys.path.insert(0, str(REPO_ROOT / "skills" / "log-nutrition" / "evals"))

import step4_benchmark  # noqa: E402


def test_step4_benchmark_covers_required_fixtures_and_preserves_live_db():
    result = step4_benchmark.run_benchmark(repetitions=1)
    assert result["fixture_counts"] == {"replay": 11, "retrieval": 20, "p0_executable": 3}
    assert result["live_db_integrity"]["unchanged"] is True
    assert "path" not in result["live_db_integrity"]
    assert result["latency"]["current_shared_writer_p0"]["sample_count"] == 3
    assert result["latency"]["old_pre_retrieval_lookup"]["sample_count"] == 20
    assert result["latency"]["current_nutrition_retrieve"]["sample_count"] == 20
    assert result["aggregates"]["current"]["candidate_correct"] == 20
    assert result["aggregates"]["current"]["candidate_total"] == 20
    assert result["aggregates"]["current"]["replay_correct"] == 11
    assert result["aggregates"]["current"]["replay_total"] == 11
    assert result["aggregates"]["current"]["decision_correct"] == result["aggregates"]["current"]["decision_total"]
    assert result["aggregates"]["current"]["duplicates"] == 0
    assert result["aggregates"]["current"]["unsafe_fuzzy_writes"] == 0
    assert result["aggregates"]["current"]["recipe_precedence_errors"] == 0
    assert result["aggregates"]["old"]["retrieval_calls"] > 0
    assert result["aggregates"]["current"]["local_benchmark_network_calls"] == 0
    assert result["aggregates"]["current"]["full_agent_usda_web_calls"] is None
    assert result["unavailable"]["model_calls"]["value"] is None
    assert "LLM/full agent" in result["unavailable"]["model_calls"]["reason"]


def test_old_lookup_is_labeled_sql_only_and_weaker_than_current(tmp_path: Path):
    db, profile = step4_benchmark.create_isolated_db(tmp_path)
    old = step4_benchmark.old_pre_retrieval_lookup(db, {"terms": ["avacado"]})
    current = step4_benchmark.retrieve_nutrition(db, {"terms": ["avacado"], "profile_path": str(profile)})
    assert old["retrieval_calls"] == 1
    assert old["writes"] == 0
    assert old["usda_web_calls"] is None
    assert not any(c["label"].casefold() == "avocado" for c in old["candidates"])
    assert any(c["candidate_type"] == "ingredient_basis" and c["label"].casefold() == "avocado" for c in current["candidates"])


def test_writer_latency_uses_every_p0_case_for_every_repetition():
    result = step4_benchmark.run_benchmark(repetitions=2)
    assert result["latency"]["current_shared_writer_p0"]["sample_count"] == 6
    # Scored once per P0 fixture even though writer latency repeats.
    assert result["aggregates"]["current"]["writes"] == 2
    assert result["aggregates"]["current"]["decision_total"] == 22


def test_cli_does_not_write_tracked_results_by_default(tmp_path: Path):
    tracked = REPO_ROOT / "skills" / "log-nutrition" / "evals" / "step4_results.json"
    before = tracked.read_bytes() if tracked.exists() else None
    artifact = tmp_path / "step4.json"
    proc = subprocess.run(
        [sys.executable, str(BENCH), "--repetitions", "1", "--results-path", str(artifact)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(artifact.read_text())
    assert payload["fixture_counts"]["retrieval"] == 20
    after = tracked.read_bytes() if tracked.exists() else None
    assert after == before
