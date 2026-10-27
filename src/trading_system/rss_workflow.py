"""Bounded RSS read, offline storage and audit; no order or recovery actions."""

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb

from trading_system.data_sources.rss_live import observe
from trading_system.monitoring.rss import audit, audit_runs
from trading_system.storage.rss import RSSStore
from trading_system.storage.store import atomic_write, canonical


def execute(
    *,
    workbook: Path | None = None,
    run: Path | None = None,
    code: str = "",
    runs_root: Path = Path("data/rss-live"),
    store_root: Path = Path("data/rss-store"),
    reports_root: Path = Path("data/rss-workflows"),
    samples: int = 3,
    interval: float = 2,
    timeout: float = 10,
) -> tuple[Path, dict]:
    if (workbook is None) == (run is None):
        raise ValueError("choose_capture_or_saved_run")
    report_path = reports_root / f"{uuid4().hex}.json"
    report = {
        "schema_version": 1,
        "status": "running",
        "stage": "prepare",
        "started_at": datetime.now(UTC).isoformat(),
        "completed_stages": [],
        "mode": "capture" if workbook else "saved_run",
        "rss_connection_status": "unknown",
        "freshness_verified": False,
    }

    def persist():
        atomic_write(report_path, canonical(report).encode("utf-8"))

    def stage(name):
        report["stage"] = name
        persist()

    try:
        persist()
        if workbook is not None:
            stage("observe")
            run, summary = observe(
                workbook, code, runs_root, samples=samples, interval=interval, timeout=timeout
            )
            report["observation"] = summary
            report["completed_stages"].append("observe")
        stage("validate_run")
        if not run.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", run.name):
            raise ValueError("invalid_run")
        report["run_id"] = run.name
        path = run / "summary.json"
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("summary_too_large")
        summary = json.loads(path.read_bytes())
        count = summary["samples"]
        if (
            type(count) is not int
            or not 1 <= count <= 60
            or summary.get("schema_version") != 1
            or summary.get("status") not in ("observed", "attention_required")
        ):
            raise ValueError("invalid_summary")
        paths = sorted(p.name for p in run.iterdir() if re.fullmatch(r"\d{3}\.json", p.name))
        if paths != [f"{i:03d}.json" for i in range(count)]:
            raise ValueError("incomplete_run")
        report["completed_stages"].append("validate_run")
        stage("import")
        with RSSStore(store_root) as store:
            report["import"] = store.import_run(run)
            report["completed_stages"].append("import")
            stage("export")
            report["export"] = store.export()
            report["completed_stages"].append("export")
        stage("audit")
        report["audit"] = audit(store_root)
        report["coverage"] = audit_runs(store_root, run.parent)
        report["completed_stages"].append("audit")
        attention = (
            summary["status"] != "observed"
            or report["import"]["attention"] > 0
            or report["audit"]["status"] != "consistent"
            or report["coverage"]["status"] != "consistent"
        )
        report["status"] = "attention_required" if attention else "complete"
        report["stage"] = "finished"
    except (OSError, ValueError, TypeError, KeyError, AttributeError, duckdb.Error):
        report["status"] = "failed"
        report["reason"] = "rss_workflow_failed"
    report["finished_at"] = datetime.now(UTC).isoformat()
    persist()
    return report_path, report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--workbook", type=Path)
    mode.add_argument("--run", type=Path)
    parser.add_argument("--code", default="")
    parser.add_argument("--runs-root", type=Path, default=Path("data/rss-live"))
    parser.add_argument("--store-root", type=Path, default=Path("data/rss-store"))
    parser.add_argument("--reports-root", type=Path, default=Path("data/rss-workflows"))
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)
    if args.workbook is not None and not args.code:
        parser.error("--code is required with --workbook")
    try:
        path, report = execute(**vars(args))
    except (OSError, ValueError):
        print(json.dumps({"status": "failed", "reason": "workflow_report_unavailable"}))
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "stage": report["stage"],
                "report": str(path),
                "run_id": report.get("run_id"),
            }
        )
    )
    return {"complete": 0, "attention_required": 3, "failed": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
