"""One local 20:10 paper recovery for the desktop and the scheduled evening heartbeat."""

from datetime import time
from pathlib import Path

from trading_system import plan_recovery as recovery
from trading_system.brokers.locking import single_writer
from trading_system.research.daytrade_universe import calendar_days


def collect(project, now):
    from trading_system.plan_startup import run_startup

    return run_startup(project, now=now)


def legacy_result(report, day):
    """Convert actual per-source observations into the existing evening job's result fields."""
    payload = report.get("supplemental", {})
    if payload.get("target") != day:
        payload = report.get("recovery", {})
    if payload.get("target") != day:
        return {}
    steps = payload.get("steps", {})
    ok = ("ready", "recovered")

    def field(key):
        return steps.get(key, {})

    def dated(key):
        dates = field(key).get("dates", {})
        return {
            d: (
                "acquired"
                if r["status"] == "recovered"
                else "already_acquired"
                if r["status"] == "ready"
                else "needs_review:" + r.get("reason", "unknown")
                if r["status"] == "error"
                else r["status"]
            )
            for d, r in sorted(dates.items())[-2:]
        }

    output = {"credit": dated("jsf"), "short_margin": dated("margin")}
    step = field("earnings")
    if step.get("status") in ok:
        output["earnings"] = {
            "state": "captured" if step["status"] == "recovered" else "verified",
            **{k: step[k] for k in ("manifest", "available_at", "files", "companies") if k in step},
        }
    else:
        output["earnings"] = {
            "state": "needs_review",
            "reason": step.get("reason", "missing_receipt"),
        }
    step = field("short_public")
    output["short_public"] = (
        "acquired"
        if step.get("status") == "recovered"
        else "already_acquired"
        if step.get("status") == "ready"
        else "needs_review:" + step.get("reason", step.get("status", "missing_receipt"))
    )
    step = field("legacy_close")
    output["settled"] = step.get("settled", {"needs_review": step.get("reason", "missing_receipt")})
    step = field("review")
    if step.get("status") in ok and "file" in step and "matched_credit" in step:
        output["review"] = {"file": step["file"], "matched_credit": step["matched_credit"]}
    return output


def run_evening(project, *, now=None):
    project = Path(project).resolve()
    tick = recovery.clock(now)
    current = tick()
    day = current.date().isoformat()
    due = recovery.at(day, time(20, 10))
    if current < due:
        return {"state": "not_due", "status": "waiting", "date": day, "due_at": due.isoformat()}
    root = project / "data/plan-forward/recovery/evening"
    path = root / (day + ".json")
    try:
        with single_writer(root):
            if path.is_file():
                saved = recovery.read(path)
                if saved.get("date") != day:
                    raise ValueError("evening_receipt_date_mismatch")
                if saved["status"] == "ok":
                    return dict(saved, already_handled=True)
            receipt = dict(
                state="evening",
                date=day,
                due_at=due.isoformat(),
                started_at=tick().isoformat(),
                status="running",
            )
            recovery.save(path, receipt)
            try:
                days, _ = calendar_days(project / "data/jquants", current.date(), current)
                if days[day] in ("1", "2"):
                    report = collect(project, now)
                    if report.get("status") not in ("ok", "attention", "error", "busy"):
                        raise ValueError("invalid_evening_recovery_status")
                    receipt.update(report)
                    receipt.update(legacy_result(report, day))
                    if report.get("status") == "ok":
                        # A fully collected source may report a real empty release. Keep that
                        # result visible and retryable, without creating a completed receipt.
                        evidence = receipt.get("credit", {})
                        margins = receipt.get("short_margin", {})
                        if (
                            len(evidence) != 2
                            or len(margins) != 2
                            or "review" not in receipt
                            or isinstance(receipt.get("settled"), dict)
                            or any(
                                s not in ("acquired", "already_acquired")
                                for s in [
                                    *evidence.values(),
                                    *margins.values(),
                                    receipt.get("short_public"),
                                ]
                            )
                            or receipt.get("earnings", {}).get("state")
                            not in ("captured", "verified")
                        ):
                            receipt["status"] = "attention"
                elif days[day] in ("0", "3"):
                    receipt.update(status="ok", state="exchange_closed")
                else:
                    raise ValueError("invalid_exchange_calendar")
            except Exception as exc:
                receipt.update(status="error", reason=recovery.explain(exc))
            receipt["checked_at"] = tick().isoformat()
            if receipt["status"] == "ok":
                receipt["completed_at"] = receipt["checked_at"]
            recovery.save(path, receipt)
            return receipt
    except ValueError as exc:
        if str(exc) == "broker_state_already_open":
            return {"status": "busy", "state": "evening", "date": day}
        raise
