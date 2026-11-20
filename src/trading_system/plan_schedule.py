"""Shared 22:00 weekday schedule and recovery for a missed Windows run."""

from datetime import time

EVENING_CHECK = time(22)


def run_scheduled_close(project, *, now=None):
    from trading_system.plan_evening import run_evening
    from trading_system.plan_recovery import clock
    from trading_system.plan_startup import run_startup

    current = clock(now)()
    if current.weekday() >= 5:
        return {"status": "ok", "state": "weekend", "date": current.date().isoformat()}
    if current.time() >= EVENING_CHECK:
        return run_evening(project, now=now)
    # StartWhenAvailable may deliver last night's task after the next sign-in.
    # Startup recovery selects the last completed session using the exchange calendar.
    return run_startup(project, now=now)
