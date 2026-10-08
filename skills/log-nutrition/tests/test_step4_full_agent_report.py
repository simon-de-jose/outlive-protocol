from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REPORTER = ROOT / "skills" / "log-nutrition" / "evals" / "step4_full_agent_report.py"
ARTIFACTS = Path("/tmp/food-journal-step4-full-agent/runs")


def load_reporter():
    spec = importlib.util.spec_from_file_location("step4_full_agent_report", REPORTER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def require_artifacts():
    if not ARTIFACTS.exists():
        import pytest
        pytest.skip("Step 4 full-agent /tmp artifacts are not present")


def test_step4_full_agent_report_scores_observed_artifacts():
    require_artifacts()
    reporter = load_reporter()
    report = reporter.build_report(ARTIFACTS)

    assert report["score_summary"]["new_meets_gate"] is True
    assert report["paths"]["new"]["correctness"] == {"correct": 6, "total": 6, "rate": 1.0}
    assert report["paths"]["new"]["duplicates"] == 0
    assert report["paths"]["new"]["false_confirmations"] == 0
    assert report["paths"]["new"]["row_provider_message_id_integrity"] == {"ok": 4, "total": 4}
    assert report["paths"]["new"]["receipt_identity_integrity"] == {"ok": 4, "total": 4}
    assert report["paths"]["new"]["source_provenance_integrity"] == {"ok": 4, "total": 4}
    assert report["paths"]["new"]["retrieval_calls"] == 16
    assert report["paths"]["new"]["usda_calls"] == 0
    assert report["paths"]["new"]["web_calls"] == 2
    assert report["paths"]["new"]["write_tool_calls"] == 6

    assert report["score_summary"]["old_brand_sequence_or_identity_safety_failure"] is True
    assert report["score_summary"]["old_exact_reuse_safety_failure"] is True
    assert report["paths"]["old"]["correctness"]["correct"] == 4
    assert report["paths"]["old"]["false_confirmations"] == 0
    assert report["paths"]["old"]["row_provider_message_id_integrity"] == {"ok": 3, "total": 4}
    assert report["paths"]["old"]["receipt_identity_integrity"] == {"ok": 1, "total": 4}
    assert report["paths"]["old"]["retrieval_calls"] == 27
    assert report["paths"]["old"]["usda_calls"] == 0
    assert report["paths"]["old"]["web_calls"] == 7
    assert report["paths"]["old"]["write_tool_calls"] == 4
    old_brand = next(c for c in report["paths"]["old"]["cases"] if c["case"] == "brand")
    assert old_brand["false_confirmation"] is False
    assert old_brand["sequence_or_identity_safety_failure"] is True
    assert old_brand["observed_issue"] == "manual/anomalous entry_id allocation or missing durable identity receipt"
    assert old_brand["new_entries"][0]["entry_id"] == 1


def test_step4_full_agent_cli_stdout_and_explicit_results_path(tmp_path):
    require_artifacts()
    stdout = subprocess.check_output([sys.executable, str(REPORTER), str(ARTIFACTS)], text=True)
    report = json.loads(stdout)
    assert "run_dir_label" in report
    assert "/tmp/food-journal-step4-full-agent" not in stdout
    assert "/Users/" not in stdout

    out = tmp_path / "report.json"
    subprocess.check_call([sys.executable, str(REPORTER), str(ARTIFACTS), "--results-path", str(out)])
    assert out.exists()
    written = json.loads(out.read_text())
    assert written["score_summary"] == report["score_summary"]


def test_step4_full_agent_manifest_is_sanitized():
    manifest = ROOT / "skills" / "log-nutrition" / "evals" / "fixtures" / "step4_full_agent_manifest.json"
    text = manifest.read_text()
    assert "/tmp/" not in text
    assert "/Users/" not in text
    assert "step4-{path}-brand" in text
