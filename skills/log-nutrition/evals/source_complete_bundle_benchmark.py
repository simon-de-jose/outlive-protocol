#!/usr/bin/env python3
"""Deterministic local benchmark for legacy vs source-complete evidence bundles."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "skills" / "log-nutrition" / "scripts"
sys.path[:0] = [str(REPO_ROOT), str(SCRIPTS_DIR)]
import nutrition_evidence_bundle as neb  # noqa: E402
from nutrition_ingest import ingest_nutrition, migrate_database  # noqa: E402

SOURCE_COMPLETE_MAX_BYTES = 16 * 1024
MAX_MEDIAN_RUNTIME_RATIO = 4.0
RUNTIME_NOISE_ALLOWANCE_MS = 25.0


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed(root: Path) -> tuple[Path, Path, Path, int]:
    db = root / "benchmark.duckdb"; profile = root / "profile.yaml"; kb = root / "recipes"; kb.mkdir()
    migrate_database(db)
    profile.write_text("nutrition_defaults:\n  latte:\n    milk_g: 200\n", encoding="utf-8")
    (kb / "house-bread.md").write_text("---\ntitle: House Bread\n---\n# House Bread\n\n## Formula\n- flour: 500 g\n- water: 350 g\n\n## Yield\n650 g\n", encoding="utf-8")
    seeded = ingest_nutrition(db, {
        "meal_time": "2026-08-20T13:00:00", "meal_type": "lunch", "meal_name": "Homemade latte and bread",
        "food_items": [{"item": "homemade latte", "portion_g": 200, "calories": 127, "protein_g": 6.4, "source": "house basis"}],
        "calories": 200, "protein_g": 8, "carbs_g": 30, "fat_total_g": 6, "source": "fixture",
    }, identity=("benchmark-fixture", "history-latte"))
    return db, profile, kb, int(seeded["result"]["entry"]["entry_id"])


def _measure(call: Callable[[], dict[str, Any]], samples: int) -> tuple[dict[str, Any], dict[str, Any]]:
    timings = []
    outputs = []
    for _ in range(samples):
        start = time.perf_counter_ns(); result = call(); timings.append((time.perf_counter_ns() - start) / 1_000_000)
        outputs.append(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    if len(set(outputs)) != 1:
        raise AssertionError("benchmark output is not deterministic")
    return result, {"sample_count": samples, "median_ms": statistics.median(timings), "max_ms": max(timings), "compact_json_bytes": len(outputs[0])}


def run_benchmark(samples: int = 21) -> dict[str, Any]:
    if samples < 5:
        raise ValueError("at least 5 samples are required for a meaningful median/max report")
    with tempfile.TemporaryDirectory(prefix="nutrition-source-complete-") as raw:
        root = Path(raw); db, profile, kb, entry_id = _seed(root); neb.KB_RECIPE_ROOT = kb
        tracked = [db, profile, *sorted(kb.glob("*.md"))]; before = {str(path.relative_to(root)): _sha(path) for path in tracked}
        legacy_request = {"db": str(db), "profile_path": str(profile), "queries": [{"terms": ["homemade latte"], "entry_ids": [entry_id]}]}
        current_request = {
            "db": str(db), "profile_path": str(profile),
            "queries": [{"terms": ["homemade latte"], "context": {"meal_type": "lunch"}}],
            "entry_requests": [{"entry_id": entry_id, "item_terms": ["latte"], "fields": ["item", "portion_g", "calories", "protein_g", "source"]}],
            "profile_keys": ["nutrition_defaults.latte"],
            "kb_recipe_queries": [{"query": "house bread", "sections": ["Formula", "Yield"]}],
        }
        legacy_result, legacy_metrics = _measure(lambda: neb.nutrition_evidence_bundle(legacy_request), samples)
        current_result, current_metrics = _measure(lambda: neb.nutrition_evidence_bundle(current_request), samples)
        after = {str(path.relative_to(root)): _sha(path) for path in tracked}
        legacy_bytes = legacy_metrics["compact_json_bytes"]
        source_bytes = current_metrics["compact_json_bytes"]
        legacy_median = legacy_metrics["median_ms"]
        source_median = current_metrics["median_ms"]
        size_delta = source_bytes - legacy_bytes
        runtime_delta = source_median - legacy_median
        size_gate = source_bytes <= SOURCE_COMPLETE_MAX_BYTES
        runtime_limit = legacy_median * MAX_MEDIAN_RUNTIME_RATIO + RUNTIME_NOISE_ALLOWANCE_MS
        runtime_gate = source_median <= runtime_limit
        correctness_gate = legacy_result["status"] == current_result["status"] == "ok"
        hash_gate = before == after
        gates = {
            "source_complete_compact_size": {
                "passed": size_gate, "observed_bytes": source_bytes, "max_bytes": SOURCE_COMPLETE_MAX_BYTES,
            },
            "median_runtime_regression": {
                "passed": runtime_gate, "observed_ms": source_median, "max_ms": runtime_limit,
                "max_ratio": MAX_MEDIAN_RUNTIME_RATIO, "noise_allowance_ms": RUNTIME_NOISE_ALLOWANCE_MS,
            },
            "path_status": {"passed": correctness_gate},
            "artifact_hashes": {"passed": hash_gate},
        }
        return {
            "status": "ok" if all(gate["passed"] for gate in gates.values()) else "failed",
            "network_calls": 0, "samples_per_path": samples,
            "paths": {"legacy_local_bundle": legacy_metrics, "source_complete_bundle": current_metrics},
            "deltas": {
                "compact_json_bytes": {
                    "bytes": size_delta,
                    "percent": (size_delta / legacy_bytes * 100.0) if legacy_bytes else None,
                },
                "median_runtime_ms": {
                    "ms": runtime_delta,
                    "percent": (runtime_delta / legacy_median * 100.0) if legacy_median else None,
                },
            },
            "interpretation": "The paths perform different work; these deltas are a regression guard, not a performance-win claim.",
            "gates": gates,
            "source_complete_coverage": current_result["coverage"],
            "source_complete_kb_budget": current_result["kb_packet_budget"],
            "temp_artifacts_unchanged": hash_gate,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--samples", type=int, default=21); parser.add_argument("--results-path", type=Path)
    args = parser.parse_args(argv); result = run_benchmark(args.samples); text = json.dumps(result, indent=2, sort_keys=True)
    if args.results_path: args.results_path.write_text(text + "\n", encoding="utf-8")
    print(text); return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
