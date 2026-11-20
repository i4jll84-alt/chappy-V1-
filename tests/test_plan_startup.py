import json
import sys
from datetime import datetime
from types import SimpleNamespace

from trading_system import plan_forward, plan_startup
from trading_system.brokers.locking import single_writer


def test_launcher_advances_plans_before_supplemental_jobs(tmp_path, monkeypatch, capsys):
    calls = []
    target = "2026-11-19"

    def recover(project, *, now, on_a_inputs):
        calls.append("fetch")
        return {"status": "attention", "target": target}

    def due(project, *, now, recovery_signal):
        assert project == tmp_path and recovery_signal == target
        calls.append("plans")
        return {"status": "ok", "plans": {"A": {}, "B": {}}}

    def supplemental(project, day, *, now):
        assert day == target
        calls.append("supplemental")
        return {"status": "ok"}

    monkeypatch.setattr(plan_startup.plan_recovery, "recover", recover)
    monkeypatch.setattr(plan_startup.plan_recovery, "recover_supplemental", supplemental)
    monkeypatch.setattr(plan_startup, "run_due", due)
    assert plan_forward.main(["--project", str(tmp_path), "--catch-up"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "attention" and set(report["plan_forward"]["plans"]) == {"A", "B"}
    assert calls == ["fetch", "plans", "supplemental"]


def test_one_startup_at_a_time_and_retry_after_process_exit(tmp_path, monkeypatch):
    root = tmp_path / "data/plan-forward/recovery/dispatch"
    with single_writer(root):
        assert plan_startup.run_startup(tmp_path)["status"] == "busy"

    def fail(*args, **kwargs):
        raise ValueError("private provider argument")

    monkeypatch.setattr(plan_startup.plan_recovery, "recover", fail)
    monkeypatch.setattr(plan_startup, "run_due", lambda *a, **k: {"status": "ok"})
    assert plan_startup.run_startup(tmp_path) == {
        "recovery": {"status": "error", "error_type": "ValueError"},
        "plan_forward": {"status": "ok"},
        "status": "error",
    }


def test_morning_result_is_recorded_before_supplemental_reports(tmp_path, monkeypatch):
    day, entry = "2026-11-19", "2026-11-20"
    events = []

    def inputs(*args, **kwargs):
        return {"status": "ok", "target": day, "next_session": entry}

    def record(project, report, *, now):
        assert report["recovery"]["target"] == day
        assert report["plan_forward"]["plans"]["B"]["status"] == "ready"
        events.append("read_decisions")
        return {"state": "met", "target": entry}

    def supplemental(*args, **kwargs):
        events.append("reports")
        return {"status": "ok"}

    monkeypatch.setattr(plan_startup.plan_recovery, "recover", inputs)
    monkeypatch.setattr(plan_startup.plan_recovery, "recover_supplemental", supplemental)
    monkeypatch.setattr(plan_startup, "record_startup", record)
    monkeypatch.setattr(
        plan_startup, "run_due", lambda *a, **k: {"plans": {"B": {"status": "ready"}}}
    )
    report = plan_startup.run_startup(tmp_path)
    assert report["morning_check"]["target"] == entry
    assert events == ["read_decisions", "reports"]


def test_registry_calls_preopen_adapter_only_for_eligible_pending_session(tmp_path, monkeypatch):
    plan = {
        "id": "B",
        "name": "B",
        "stage": "active",
        "adapter": "test_b",
        "version": "1",
        "start": "2026-11-19",
        "capital": 3000000,
    }
    monkeypatch.setattr(plan_forward, "catalog", lambda project: [plan])
    calls = []

    def recover(project, book, *, signal_date, start, capital, now):
        calls.append(signal_date)
        return {"state": "ready"}

    module = SimpleNamespace(
        recover_morning=recover,
        advance=lambda *a, **k: dict(status="ready", initial_capital=3000000),
    )
    monkeypatch.setattr(plan_forward.importlib, "import_module", lambda _: module)
    now = datetime(2026, 11, 20, 8, tzinfo=plan_forward.JST)
    for target in [None, "2026-11-18", "2026-11-20", "2026-11-19"]:
        result = plan_forward.run_due(tmp_path, now=now, recovery_signal=target)
        assert result["status"] == "ok"
    assert calls == ["2026-11-19"]
    assert result["plans"]["B"]["morning_recovery"] == {"state": "ready"}


def test_catch_up_cli_preserves_attention(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(plan_startup, "run_startup", lambda *a, **k: {"status": "attention"})
    assert plan_forward.main(["--project", str(tmp_path), "--catch-up"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "attention"


def test_desktop_evening_check_dispatches_to_ab_recovery(tmp_path, monkeypatch, capsys):
    calls = []

    def shared(project):
        calls.append(project)
        return {"status": "ok", "state": "evening"}

    monkeypatch.setitem(
        sys.modules, "trading_system.plan_evening", SimpleNamespace(run_evening=shared)
    )
    assert plan_forward.main(["--project", str(tmp_path), "--evening-check"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "ok", "state": "evening"}
    assert calls == [tmp_path]
