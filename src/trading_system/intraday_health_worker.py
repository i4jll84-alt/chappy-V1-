"""Independent, explicitly authorized health-recovery workers; credentials stay in memory."""

import argparse
import json
import sqlite3
import time
import traceback
from pathlib import Path

from trading_system import desktop_launcher as launcher
from trading_system import intraday_health as health


def run(project, broker, *, clock=health.now):
    root = project / "data" / health.FOLDERS[broker]
    moment = clock()
    if (project / "data/intraday-health/STOP").exists():
        return "disabled"
    if moment.weekday() >= 5 or not health.walltime(9, 30) <= moment.time() < health.walltime(
        15, 30
    ):
        return "outside_checks"
    if not health.calendar_state(project, moment):
        return "market_closed"
    if any((root / name).exists() for name in ("STOP", "STOP.resuming")):
        return "manual_stop"
    active = launcher.lock_active(root / "writer.lock")
    if broker == "gui":
        if not active:
            launcher.main()
        return "desktop_present"
    settings = health.read_json(project / "data/desktop-launcher/settings.json")
    settings = launcher.validate_settings(settings) if settings else launcher.default_settings()
    source, field = ("rakuten", "rakuten_app") if broker == "panel" else ("mufg_esmart", "mufg_app")
    launcher.ensure_broker_app(project, source, Path(settings[field]))
    if active:
        status = health.read_json(root / "status.json", {})
        if broker == "panel" and status.get("reason") in health.REOPEN:
            import os

            from trading_system.research.daytrade_universe import panel_location
            from trading_system.research_panel import configuration

            _, book = configuration(panel_location(root, moment.date()))
            os.startfile(str(book))
            return "workbook_open_requested"
        current = health.snapshot(project, moment)["components"][broker]
        stale = any(
            x in current["issues"] for x in ("saved_observations_stale", "market_data_stale")
        )
        if status.get("state") in health.WAITING or not stale:
            return "collector_already_active"
        if moment.time() >= health.walltime(15, 25):
            return "close_auction_restart_deferred"
        from trading_system.capture_control import request_restart

        request_restart(root, moment)
        for _ in range(60):
            if not launcher.lock_active(root / "writer.lock"):
                break
            time.sleep(1)
        else:
            return "cooperative_restart_pending"
        if (root / "STOP").exists() or (root / "STOP.resuming").exists():
            return "manual_stop"
        if clock().time() >= health.walltime(15, 30):
            return "outside_checks"
    if broker == "panel":
        from trading_system.research.paired_start import main

        main()
    else:
        from trading_system import kabu_study
        from trading_system.research.shared_market import after_stop

        kabu_study.run(project, time.time_ns(), "none")
        after_stop(project, moment.date(), "mufg_esmart")
    return "collector_completed"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", choices=list(health.TASKS), required=True)
    args = parser.parse_args(argv)
    project = health.PROJECT
    report = {"broker": args.broker, "started_at": health.now().isoformat()}
    try:
        report["state"] = run(project, args.broker)
    except Exception as exc:
        # Keep diagnostics without exception messages, tokens, passwords or locals.
        report.update(
            state="failed",
            error_type=type(exc).__name__,
            sqlite_errorname=getattr(exc, "sqlite_errorname", None)
            if isinstance(exc, sqlite3.Error)
            else None,
            frames=[
                {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                for f in traceback.extract_tb(exc.__traceback__)
            ],
        )
    report["finished_at"] = health.now().isoformat()
    folder = project / "data/intraday-health/workers" / args.broker
    health.save(folder / (health.now().strftime("%Y%m%dT%H%M%S-%f") + ".json"), report)
    health.save(folder / "latest.json", report)
    print(json.dumps(report, ensure_ascii=False))
    return 2 if report["state"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
