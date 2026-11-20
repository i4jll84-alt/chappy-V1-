import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from trading_system import (
    plan_evening,
    plan_forward,
    plan_schedule,
    plan_startup,
)
from trading_system import (
    plan_recovery as r,
)


def local(stamp):
    return datetime.fromisoformat(stamp + "+09:00")


@pytest.mark.parametrize(
    "stamp,action",
    [
        ("2026-11-20T22:00:00", "evening"),
        ("2026-11-20T23:00:00", "evening"),
        ("2026-11-24T07:40:00", "startup"),
        ("2026-11-24T09:01:00", "startup"),
        ("2026-11-24T21:59:59", "startup"),
        ("2026-11-21T07:40:00", "weekend"),
        ("2026-11-22T22:00:00", "weekend"),
    ],
)
def test_windows_schedule_and_delayed_sign_in_share_existing_safe_paths(
    tmp_path, monkeypatch, stamp, action
):
    evening = Mock(return_value={"status": "ok", "state": "evening"})
    startup = Mock(return_value={"status": "ok", "state": "startup"})
    monkeypatch.setattr(plan_evening, "run_evening", evening)
    monkeypatch.setattr(plan_startup, "run_startup", startup)
    now = local(stamp)
    result = plan_schedule.run_scheduled_close(tmp_path, now=now)
    assert result["state"] == action
    if action == "evening":
        evening.assert_called_once_with(tmp_path, now=now)
        startup.assert_not_called()
    elif action == "startup":
        startup.assert_called_once_with(tmp_path, now=now)
        evening.assert_not_called()
    else:
        evening.assert_not_called()
        startup.assert_not_called()


def test_scheduled_cli_preserves_failure_for_windows_retry(tmp_path, monkeypatch, capsys):
    run = Mock(return_value={"status": "attention"})
    monkeypatch.setattr(plan_schedule, "run_scheduled_close", run)
    assert plan_forward.main(["--project", str(tmp_path), "--scheduled-close"]) == 2
    run.assert_called_once_with(tmp_path)
    assert json.loads(capsys.readouterr().out)["status"] == "attention"


@pytest.mark.parametrize(
    "target,next_day", [("2026-11-19", "2026-11-20"), ("2026-11-20", "2026-11-24")]
)
def test_failed_night_recovers_missing_daily_before_morning_paper_work(
    tmp_path, monkeypatch, target, next_day
):
    active = {"now": local(target + "T22:00:00"), "offline": True}
    events = []
    receipt = tmp_path / "data/jquants/daily-snapshots" / target / "last_collection.json"
    monkeypatch.setattr(plan_evening, "calendar_days", lambda root, day, now: ({str(day): "1"}, []))
    monkeypatch.setattr(r, "coverage", lambda *a: (target, next_day, [target], [target]))
    settings = SimpleNamespace(
        data_root=Path("data/jquants"),
        model_copy=lambda **k: SimpleNamespace(**k["update"]),
    )
    monkeypatch.setattr(r.Settings, "load", lambda *a: settings)
    acquired = []

    def collect(day, settings, *, run_paper):
        assert str(day) == target and not run_paper
        if active["offline"]:
            raise OSError("offline fixture")
        acquired.append(target)
        events.append("daily")
        r.save(
            receipt,
            {
                "date": target,
                "state": "complete",
                "daily_rows": 1,
                "daily_manifest": "fixture",
                "completed_at": active["now"].isoformat(),
                "jobs": {
                    key: {"state": "complete"}
                    for key in ("calendar", "master", "topix", "financials")
                },
            },
        )

    monkeypatch.setattr(r.swing_daily, "collect", collect)
    monkeypatch.setattr(r, "dated_inputs", lambda *a: None)
    monkeypatch.setattr(r.a0_5_inputs, "market_pages", lambda *a: None)
    for name in ("jsf", "schedule_input", "margins", "flow", "evening_b"):
        monkeypatch.setattr(r, name, lambda *a: {"status": "ready"})

    def due(project, *, now, recovery_signal=None):
        if not receipt.exists():
            events.append("paper_waiting")
            return {"status": "attention", "plans": {}}
        assert r.read(receipt)["completed_at"] == active["now"].isoformat()
        assert now >= active["now"]
        events.append("paper_ready")
        a = dict(
            start=target,
            status="ready",
            next_entry=next_day,
            last_decision=target,
            decision_status="ready",
            decision_recorded_at=now.isoformat(),
        )
        return {"status": "ok", "plans": {"A": a, "B": {"status": "ready"}}}

    monkeypatch.setattr(plan_startup, "run_due", due)
    monkeypatch.setattr(plan_startup, "record_startup", lambda *a, **k: {"state": "met"})
    monkeypatch.setattr(
        r, "recover_supplemental", lambda *a, **k: {"status": "ok", "target": target}
    )

    failed = plan_schedule.run_scheduled_close(tmp_path, now=active["now"])
    assert failed["status"] == "attention" and not receipt.exists()
    assert events == ["paper_waiting"]
    active.update(now=local(next_day + "T07:40:00"), offline=False)
    events.clear()
    recovered = plan_schedule.run_scheduled_close(tmp_path, now=active["now"])
    assert recovered["status"] == "ok"
    assert recovered["recovery"]["steps"]["daily"]["status"] == "recovered"
    assert events[0] == "daily" and events[1:] == ["paper_ready", "paper_ready"]
    assert set(recovered["plan_forward"]["plans"]) == {"A", "B"}
    assert plan_schedule.run_scheduled_close(tmp_path, now=active["now"])["status"] == "ok"
    assert acquired == [target]
