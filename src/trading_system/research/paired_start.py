"""Start the operator-authorized paired Rakuten recorder on exchange sessions."""

import json
import time
from datetime import datetime
from datetime import time as clock
from pathlib import Path

from trading_system import desktop_launcher as launcher
from trading_system import research_panel
from trading_system.research.daytrade_universe import calendar_days, panel_location
from trading_system.research.shared_market import after_stop


def main():
    project = research_panel.PROJECT
    root = project / "data/rss-panel"
    now = datetime.now(research_panel.JST)
    if not clock(6) <= now.time() < clock(15, 59) or launcher.collector_active(project, "panel"):
        return 0
    day = now.date()
    if (root / "STOP").exists() or (root / "STOP.resuming").exists():
        return 0
    cal, _ = calendar_days(project / "data/jquants", day, now)
    if cal[str(day)] not in ("1", "2"):
        return 0
    folder = panel_location(root, day)
    if folder == root:
        return 0
    settings = project / "data/desktop-launcher/settings.json"
    config = (
        launcher.validate_settings(json.loads(settings.read_bytes()))
        if settings.exists()
        else launcher.default_settings()
    )
    launcher.ensure_broker_app(project, "rakuten", Path(config["rakuten_app"]))
    try:
        research_panel.run(root, time.time_ns(), "none")
    except ValueError as exc:
        if str(exc) == "broker_state_already_open":
            return 0
        raise
    after_stop(project, day, "rakuten")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
