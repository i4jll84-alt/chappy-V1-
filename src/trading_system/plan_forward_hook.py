"""Advance prospective paper accounts after the existing scheduled market jobs."""

import logging


def _advance(project):
    try:
        from trading_system.plan_forward import run_due

        return run_due(project)
    except Exception as exc:
        # Never log provider arguments, URLs or credentials.
        logging.getLogger(__name__).error("Paper plan update failed (%s)", type(exc).__name__)
        return {"status": "error", "error_type": type(exc).__name__}


def run_scheduled(project, job):
    """Refresh shared inputs first. A failure in the legacy job still permits other plans to run."""
    try:
        report = job()
    except Exception:
        _advance(project)
        raise
    return {**report, "plan_forward": _advance(project)}


def run_morning(project, job):
    """Run time-sensitive decisions before closing the books and producing reports."""
    from trading_system.plan_startup import attempt, finish_startup, run_startup

    pending = attempt(run_startup, project, supplemental=False)
    try:
        legacy = job()
    finally:
        completed = attempt(finish_startup, project, pending)
    return {
        **legacy,
        "plan_forward": pending.get("plan_forward", pending),
        "evening_recovery": completed,
    }
