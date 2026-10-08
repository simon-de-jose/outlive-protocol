#!/usr/bin/env python3
"""Stable cron entrypoint for health data sync."""

from __future__ import annotations

import argparse
import contextlib
import io
import multiprocessing as mp
import queue
import subprocess
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

from bootstrap.env import db_path, icloud_folder, log_dir  # noqa: E402
from daily_import import move_imported_files, print_summary, run_daily_import  # noqa: E402
from sync_libre import sync_libre  # noqa: E402
from validate import run_validation  # noqa: E402


DEFAULT_HEALTHKIT_MAX_AGE_HOURS = 48.0
DEFAULT_LIBRE_MAX_AGE_HOURS = 8.0


@dataclass(frozen=True)
class SourceFreshness:
    source: str
    latest: datetime | None
    age_hours: float | None
    max_age_hours: float

    @property
    def is_fresh(self) -> bool:
        return self.age_hours is not None and self.age_hours <= self.max_age_hours


def capture(func, *args, **kwargs):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        result = func(*args, **kwargs)
    return result, buffer.getvalue()


def _capture_worker(out_queue, func, args, kwargs):
    try:
        result, output = capture(func, *args, **kwargs)
        out_queue.put(("ok", result, output))
    except BaseException as exc:  # child process: return traceback to parent
        out_queue.put(("error", repr(exc), traceback.format_exc()))


def capture_with_timeout(func, timeout_seconds: int, *args, **kwargs):
    """Run a capture target in a child process so wedged iCloud scans cannot hang cron."""
    ctx = mp.get_context("fork")
    out_queue = ctx.Queue()
    proc = ctx.Process(target=_capture_worker, args=(out_queue, func, args, kwargs))
    proc.start()
    proc.join(timeout_seconds)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        raise TimeoutError(f"{func.__name__} timed out after {timeout_seconds}s")
    try:
        status, result, output = out_queue.get_nowait()
    except queue.Empty:
        if proc.exitcode == 0:
            return None, ""
        raise RuntimeError(f"{func.__name__} exited with code {proc.exitcode}")
    if status == "error":
        raise RuntimeError(f"{func.__name__} failed: {result}\n{output}")
    return result, output


def run_brctl(path: Path, timeout_seconds: int = 20) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["brctl", "download", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return 124, f"brctl download timed out after {timeout_seconds}s"
    output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
    return result.returncode, output


def validate_counts() -> tuple[list[tuple[str, int]], str]:
    import duckdb

    db = duckdb.connect(str(db_path()), read_only=True)
    try:
        rows = db.sql(
            "SELECT source, COUNT(*) FROM readings GROUP BY source ORDER BY source"
        ).fetchall()
    finally:
        db.close()
    return rows, repr(rows)


def _age_hours(latest: datetime, now: datetime) -> float:
    """Calculate age while accommodating DuckDB's naive TIMESTAMP values."""
    if latest.tzinfo is None:
        comparable_now = now.replace(tzinfo=None)
    elif now.tzinfo is None:
        comparable_now = now.astimezone().astimezone(latest.tzinfo)
    else:
        comparable_now = now.astimezone(latest.tzinfo)
    return max(0.0, (comparable_now - latest).total_seconds() / 3600)


def check_db_freshness(
    healthkit_max_age_hours: float,
    libre_max_age_hours: float,
    *,
    now: datetime | None = None,
) -> dict[str, SourceFreshness]:
    """Read each source's newest timestamp and compare it with its threshold."""
    import duckdb

    thresholds = {
        "healthkit": healthkit_max_age_hours,
        "libre": libre_max_age_hours,
    }
    db = duckdb.connect(str(db_path()), read_only=True)
    try:
        rows = db.execute(
            """
            SELECT source, MAX(timestamp)
            FROM readings
            WHERE source IN ('healthkit', 'libre')
            GROUP BY source
            """
        ).fetchall()
    finally:
        db.close()

    latest_by_source = {source: latest for source, latest in rows}
    checked_at = now or datetime.now().astimezone()
    return {
        source: SourceFreshness(
            source=source,
            latest=latest_by_source.get(source),
            age_hours=(
                _age_hours(latest_by_source[source], checked_at)
                if latest_by_source.get(source) is not None
                else None
            ),
            max_age_hours=max_age,
        )
        for source, max_age in thresholds.items()
    }


def _freshness_text(freshness: SourceFreshness) -> str:
    if freshness.latest is None:
        return f"DB has no {freshness.source} readings"
    state = "fresh" if freshness.is_fresh else "stale"
    return (
        f"DB {state}, latest {freshness.latest:%Y-%m-%d %H:%M}, "
        f"{freshness.age_hours:.1f}h old (limit {freshness.max_age_hours:g}h)"
    )


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the health sync cron pipeline")
    parser.add_argument(
        "--skip-icloud",
        action="store_true",
        help="Skip brctl download; useful for tests on non-macOS hosts.",
    )
    parser.add_argument(
        "--healthkit-timeout",
        type=int,
        default=45,
        help="Seconds to allow each HealthKit/iCloud file operation.",
    )
    parser.add_argument(
        "--healthkit-max-age-hours",
        type=_nonnegative_float,
        default=DEFAULT_HEALTHKIT_MAX_AGE_HOURS,
        help=f"Fail when HealthKit's newest DB timestamp is older than this (default: {DEFAULT_HEALTHKIT_MAX_AGE_HOURS:g}).",
    )
    parser.add_argument(
        "--libre-max-age-hours",
        type=_nonnegative_float,
        default=DEFAULT_LIBRE_MAX_AGE_HOURS,
        help=f"Fail when Libre's newest DB timestamp is older than this (default: {DEFAULT_LIBRE_MAX_AGE_HOURS:g}).",
    )
    args = parser.parse_args(argv)

    now = datetime.now().astimezone()
    display_date = now.strftime("%-m/%-d")
    logfile = log_dir() / f"{now:%Y-%m-%d}-health-import.log"
    # iCloud download/scan failures are warnings while the persisted HealthKit
    # data is still within its freshness window.  The freshness check below is
    # the authoritative health signal; otherwise a transient iCloud outage
    # would repeatedly fail and disable the whole (including Libre) pipeline.
    healthkit_warnings: list[str] = []
    healthkit_issues: list[str] = []
    libre_issues: list[str] = []
    pipeline_issues: list[str] = []

    log_parts = [
        f"Health import run: {now:%Y-%m-%d %H:%M:%S %Z}".rstrip(),
        f"Repo: {REPO_ROOT}",
        f"DB: {db_path()}",
        f"iCloud folder: {icloud_folder()}",
        (
            "Freshness limits: "
            f"healthkit={args.healthkit_max_age_hours:g}h, "
            f"libre={args.libre_max_age_hours:g}h"
        ),
        "",
    ]

    if args.skip_icloud:
        log_parts.extend(["[Step 1] iCloud sync skipped", ""])
    else:
        rc, output = run_brctl(icloud_folder())
        log_parts.extend(["[Step 1] Force iCloud sync", output, f"brctl exit: {rc}", ""])
        if rc != 0:
            healthkit_warnings.append(f"brctl failed ({rc})")

    daily_stats = {
        "total": 0,
        "new": 0,
        "changed": 0,
        "skipped": 0,
        "imported": 0,
        "errors": 0,
        "rows_added": 0,
    }
    scan_succeeded = False
    try:
        result, daily_output = capture_with_timeout(
            run_daily_import, args.healthkit_timeout, dry_run=False
        )
        if result is not None:
            daily_stats = result
        scan_succeeded = True
        log_parts.extend(["[Step 2] HealthKit import", daily_output])
    except TimeoutError as exc:
        log_parts.extend(["[Step 2] HealthKit import", f"ERROR: {exc}", ""])
        healthkit_warnings.append("file scan timed out")
    except Exception as exc:
        log_parts.extend(["[Step 2] HealthKit import", traceback.format_exc(), ""])
        healthkit_warnings.append(f"import failed ({type(exc).__name__})")

    if scan_succeeded and int(daily_stats.get("errors", 0)) == 0:
        try:
            _, move_output = capture_with_timeout(
                move_imported_files, args.healthkit_timeout, dry_run=False
            )
            log_parts.extend(["[Step 2b] Move imported files", move_output])
        except TimeoutError as exc:
            log_parts.extend(["[Step 2b] Move imported files", f"ERROR: {exc}", ""])
            healthkit_warnings.append("file move timed out")
        except Exception as exc:
            log_parts.extend(["[Step 2b] Move imported files", traceback.format_exc(), ""])
            healthkit_warnings.append(f"file move failed ({type(exc).__name__})")
    elif int(daily_stats.get("errors", 0)):
        healthkit_issues.append(f"import reported {daily_stats['errors']} error(s)")

    _, summary_output = capture(print_summary, daily_stats)
    log_parts.extend(["[Step 2c] Import summary", summary_output])

    libre_result: dict = {"status": "error", "inserted": 0}
    try:
        result, libre_output = capture(sync_libre, use_graph=True, dry_run=False)
        if result is not None:
            libre_result = result
        log_parts.extend(["[Step 3] LibreView glucose sync", libre_output, repr(libre_result), ""])
        if libre_result.get("status") not in {"success", "no_readings", "no_patients"}:
            libre_issues.append(f"sync status={libre_result.get('status', 'unknown')}")
    except Exception as exc:
        log_parts.extend(["[Step 3] LibreView glucose sync", traceback.format_exc(), ""])
        libre_issues.append(f"sync failed ({type(exc).__name__})")

    try:
        validation_report, validation_output = capture(run_validation, verbose=False)
        log_parts.extend(["[Step 4] Data quality validation", validation_output])
        if validation_report:
            _, report_output = capture(validation_report.print_report, verbose=False)
            log_parts.append(report_output)
    except Exception as exc:
        log_parts.extend(["[Step 4] Data quality validation", traceback.format_exc(), ""])
        pipeline_issues.append(f"validation failed ({type(exc).__name__})")

    freshness: dict[str, SourceFreshness] = {}
    try:
        freshness = check_db_freshness(
            args.healthkit_max_age_hours,
            args.libre_max_age_hours,
            now=now,
        )
        log_parts.extend(
            [
                "[Step 5] DB freshness",
                _freshness_text(freshness["healthkit"]),
                _freshness_text(freshness["libre"]),
                "",
            ]
        )
        if not freshness["healthkit"].is_fresh:
            healthkit_issues.append(_freshness_text(freshness["healthkit"]))
        if not freshness["libre"].is_fresh:
            libre_issues.append(_freshness_text(freshness["libre"]))
    except Exception as exc:
        log_parts.extend(["[Step 5] DB freshness", traceback.format_exc(), ""])
        healthkit_issues.append(f"freshness check failed ({type(exc).__name__})")
        libre_issues.append(f"freshness check failed ({type(exc).__name__})")

    try:
        _, counts_output = validate_counts()
        log_parts.extend(["[Step 6] DB counts", counts_output, ""])
    except Exception as exc:
        log_parts.extend(["[Step 6] DB counts", traceback.format_exc(), ""])
        pipeline_issues.append(f"DB counts failed ({type(exc).__name__})")

    logfile.write_text("\n".join(log_parts), encoding="utf-8")

    files = int(daily_stats.get("imported", 0))
    rows = int(daily_stats.get("rows_added", 0))
    libre = int(libre_result.get("inserted", libre_result.get("fetched", 0)) or 0)
    healthkit_detail = f"{files} files, {rows} rows"
    libre_detail = f"{libre} new readings"
    if "healthkit" in freshness:
        healthkit_detail += f"; {_freshness_text(freshness['healthkit'])}"
    if "libre" in freshness:
        libre_detail += f"; {_freshness_text(freshness['libre'])}"

    failed = bool(healthkit_issues or libre_issues or pipeline_issues)
    # Staleness means the importer ran but the upstream HealthKit exporter has
    # not supplied a recent *reading*. Do not label that an import failure.
    healthkit_stale = (
        "healthkit" in freshness and not freshness["healthkit"].is_fresh
    )
    healthkit_status = (
        "STALE"
        if healthkit_stale
        else ("FAILED" if healthkit_issues else ("DEGRADED" if healthkit_warnings else "OK"))
    )
    libre_status = "FAILED" if libre_issues else "OK"
    issue_text = ""
    if failed:
        grouped_issues = []
        if healthkit_issues:
            grouped_issues.append(f"HealthKit: {', '.join(healthkit_issues)}")
        if healthkit_warnings:
            grouped_issues.append(f"HealthKit warning: {', '.join(healthkit_warnings)}")
        if libre_issues:
            grouped_issues.append(f"Libre: {', '.join(libre_issues)}")
        if pipeline_issues:
            grouped_issues.append(f"Pipeline: {', '.join(pipeline_issues)}")
        issue_text = f" (issues: {'; '.join(grouped_issues)}; log: {logfile})"

    icon = "🔴" if failed else ("🟡" if healthkit_warnings else "🟢")
    print(
        f"{icon} **{display_date} Health Import** — "
        f"HealthKit {healthkit_status}: {healthkit_detail} | "
        f"Libre {libre_status}: {libre_detail}{issue_text}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
