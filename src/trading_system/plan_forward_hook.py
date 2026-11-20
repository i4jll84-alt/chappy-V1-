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
    """Advance paper plans after the scheduled daily collection, including on a collection error."""
    try:
        report = job()
    except Exception:
        _advance(project)
        raise
    return {**report, "plan_forward": _advance(project)}
