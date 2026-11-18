"""Recover missing evening inputs before forward decisions and lower-priority reports."""

from pathlib import Path

from trading_system import plan_recovery
from trading_system.brokers.locking import single_writer
from trading_system.plan_forward import run_due
from trading_system.plan_morning_check import record_startup


def attempt(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception as exc:
        # Source-specific receipts contain their own diagnostic; never echo request credentials.
        return {"status": "error", "error_type": type(exc).__name__}


def combined_status(report):
    states = {step.get("status") for step in report.values() if isinstance(step, dict)}
    for status in ("error", "attention", "busy"):
        if status in states:
            return status
    return "ok"


def finish_startup(project, report, *, now=None):
    if "recovery" not in report:
        return report
    target = report.get("recovery", {}).get("target")
    if target and report["recovery"].get("status") != "busy":
        report["supplemental"] = attempt(
            plan_recovery.recover_supplemental, project, target, now=now
        )
    report["status"] = combined_status(report)
    return report


def run_startup(project, *, now=None, supplemental=True):
    project = Path(project).resolve()
    tick = plan_recovery.clock(now)
    dispatch = project / "data/plan-forward/recovery/dispatch"
    try:
        with single_writer(dispatch):

            def freeze_available(target, next_session):
                # A can freeze while the two separate B feeds are still outstanding.
                return run_due(project, now=tick())

            recovery = attempt(
                plan_recovery.recover, project, now=tick(), on_a_inputs=freeze_available
            )
            target = None if recovery.get("status") == "busy" else recovery.get("target")
            # Use newly verified inputs even when another independent feed is still missing.
            forward = attempt(run_due, project, now=tick(), recovery_signal=target)
            report = {"recovery": recovery, "plan_forward": forward}
            if target and recovery.get("next_session"):
                # Save the result as soon as decisions exist, ahead of the ancillary reports.
                report["morning_check"] = attempt(record_startup, project, report, now=tick())
            if supplemental:
                return finish_startup(project, report, now=tick())
            report["status"] = combined_status(report)
            return report
    except ValueError as exc:
        if str(exc) == "broker_state_already_open":
            return {
                "status": "busy",
                "recovery": {"status": "busy"},
                "plan_forward": {"status": "busy"},
            }
        raise
