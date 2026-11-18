import json
import sys
from datetime import date
from types import SimpleNamespace

import pytest

from trading_system import plan_forward_hook as hooks
from trading_system.research import daytrade_universe, swing_daily, swing_forward


def fake_runner(monkeypatch, due):
    monkeypatch.setitem(sys.modules, "trading_system.plan_forward", SimpleNamespace(run_due=due))


@pytest.mark.parametrize(
    "action,expected", [("morning", True), ("evening", True), ("review", False)]
)
def test_existing_heartbeat_advances_after_legacy_job(
    monkeypatch, capsys, tmp_path, action, expected
):
    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["swing_forward", action])

    def legacy(project, name, answer):
        assert project == tmp_path and name == action
        calls.append("legacy")
        return {"state": action, "input_sha256": "preserved"}

    monkeypatch.setattr(swing_forward, "run", legacy)

    def failed_plan(project):
        calls.append("plans")
        raise RuntimeError("independent paper failure")

    fake_runner(monkeypatch, failed_plan)
    assert swing_forward.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["input_sha256"] == "preserved"
    assert ("plan_forward" in report) == expected
    if expected:
        assert report["plan_forward"] == {"status": "error", "error_type": "RuntimeError"}
    assert calls == (["legacy", "plans"] if expected else ["legacy"])


@pytest.mark.parametrize("scheduled", [True, False])
def test_only_scheduled_daily_data_job_advances_plans(monkeypatch, capsys, tmp_path, scheduled):
    day = date(2026, 11, 19)
    settings = SimpleNamespace(data_root=tmp_path / "data/jquants")
    monkeypatch.setattr(swing_daily.Settings, "load", lambda: settings)
    monkeypatch.setattr(daytrade_universe, "latest_closed_session", lambda *args: day)
    calls = []

    def due(project):
        assert project == tmp_path
        calls.append("plans")
        return {"status": "attention"}

    fake_runner(monkeypatch, due)
    monkeypatch.setattr(
        swing_daily, "collect", lambda *args: calls.append("daily") or {"state": "ok"}
    )
    args = ["--latest-closed-session"] if scheduled else ["--date", day.isoformat()]
    monkeypatch.setattr(sys, "argv", ["swing_daily", *args])
    assert swing_daily.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert ("plan_forward" in report) == scheduled
    if scheduled:
        assert report["plan_forward"] == {"status": "attention"}
    assert calls == (["daily", "plans"] if scheduled else ["daily"])


def test_legacy_error_survives_if_both_jobs_fail(monkeypatch, tmp_path):
    calls = []

    def due(project):
        calls.append(project)
        raise RuntimeError("separate failure")

    def failed():
        raise ValueError("primary failure")

    fake_runner(monkeypatch, due)
    with pytest.raises(ValueError, match="primary failure"):
        hooks.run_scheduled(tmp_path, failed)
    assert calls == [tmp_path]
    result = hooks.run_scheduled(tmp_path, lambda: {"state": "saved"})
    assert result == {
        "state": "saved",
        "plan_forward": {"status": "error", "error_type": "RuntimeError"},
    }
