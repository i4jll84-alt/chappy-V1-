import gzip
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from trading_system import capture_control as control
from trading_system import intraday_health as h
from trading_system import intraday_health_worker as worker


def at(value="2026-11-24T09:30:00+09:00"):
    return datetime.fromisoformat(value)


def state(done=(), retry=False, next_at=None):
    return {
        "date": "2026-11-24",
        "mandatory_checked": list(done),
        "needs_retry": retry,
        "next_retry": next_at,
    }


@pytest.mark.parametrize(
    "moment,previous,expected",
    [
        ("2026-11-24T09:29:59+09:00", {}, "before_checks"),
        ("2026-11-24T09:30:00+09:00", {}, "due"),
        ("2026-11-24T10:00:00+09:00", state(["09:30"]), "waiting_1030"),
        ("2026-11-24T10:30:00+09:00", state(["09:30"]), "due"),
        ("2026-11-24T11:00:00+09:00", state(["09:30", "10:30"]), "finished"),
        ("2026-11-24T10:00:00+09:00", state(["09:30"], True, "2026-11-24T10:00:00+09:00"), "due"),
        (
            "2026-11-24T11:30:00+09:00",
            state(["09:30", "10:30"], True, "2026-11-24T11:30:00+09:00"),
            "lunch",
        ),
        (
            "2026-11-24T12:30:00+09:00",
            state(["09:30", "10:30"], True, "2026-11-24T12:30:00+09:00"),
            "due",
        ),
        ("2026-11-24T15:30:00+09:00", state(["09:30"], True), "session_ended"),
        ("2026-11-28T09:30:00+09:00", {}, "weekend"),
        ("2026-11-25T09:30:00+09:00", state(["09:30", "10:30"]), "due"),
    ],
)
def test_schedule_mandatory_slots_retries_lunch_close_and_reset(moment, previous, expected):
    assert h.schedule(at(moment), previous)["state"] == expected


def test_missed_slots_are_checked_now_once_without_backdating():
    assert h.schedule(at("2026-11-24T10:47:00+09:00"), {})["cover_mandatory"] == ["09:30", "10:30"]


def test_retry_grid_stops_at_lunch_and_close():
    assert h.next_retry(at("2026-11-24T09:31:20+09:00")) == "2026-11-24T10:00:00+09:00"
    assert h.next_retry(at("2026-11-24T11:01:00+09:00")) == "2026-11-24T12:30:00+09:00"
    assert h.next_retry(at("2026-11-24T15:01:00+09:00")) is None


def healthy(sequence=1):
    return {
        "healthy": True,
        "issues": [],
        "components": {
            name: {
                "active": True,
                "manual_stop": False,
                "issues": [],
                "state": "recording",
                "data": {"latest_id": sequence},
            }
            for name in h.FOLDERS
        },
    }


def test_two_healthy_checks_skip_all_additional_checks_and_keep_records(tmp_path):
    current = [at()]
    probe = Mock(side_effect=[healthy(1), healthy(2), healthy(10), healthy(11)])
    starter = Mock()
    kwargs = dict(
        clock=lambda: current[0],
        sleeper=lambda _: None,
        probe=probe,
        starter=starter,
        session_check=lambda *_: True,
    )
    first = h.run(tmp_path, **kwargs)
    assert first["state"] == "healthy" and first["next"]["next_mandatory"].endswith(
        "10:30:00+09:00"
    )
    current[0] = at("2026-11-24T10:00:00+09:00")
    assert h.run(tmp_path, **kwargs)["state"] == "waiting_1030"
    current[0] = at("2026-11-24T10:30:00+09:00")
    assert h.run(tmp_path, **kwargs)["state"] == "healthy"
    current[0] = at("2026-11-24T11:00:00+09:00")
    assert h.run(tmp_path, **kwargs)["state"] == "finished"
    assert probe.call_count == 4
    starter.assert_not_called()
    assert len(list((tmp_path / "data/intraday-health/runs/2026-11-24").glob("*.json"))) == 2


def test_repair_of_one_broker_does_not_wait_for_other_login(tmp_path):
    before, after = healthy(1), healthy(2)
    before["components"]["panel"].update(active=False, issues=["process_not_running"])
    before["components"]["kabu_panel"].update(
        state="prompt", issues=["operator_authentication_required"]
    )
    before["issues"] = ["panel:process_not_running", "kabu_panel:operator_authentication_required"]
    before["healthy"] = False
    after["components"]["kabu_panel"].update(
        state="prompt", issues=["operator_authentication_required"]
    )
    after["issues"] = ["kabu_panel:operator_authentication_required"]
    after["healthy"] = False
    starter = Mock()
    result = h.run(
        tmp_path,
        clock=at,
        sleeper=lambda _: None,
        probe=Mock(side_effect=[before, after]),
        starter=starter,
        session_check=lambda *_: True,
    )
    starter.assert_called_once_with("panel")
    assert result["next"]["needs_retry"]
    assert result["next"]["next_retry"] == "2026-11-24T10:00:00+09:00"


def test_recovered_retry_does_not_cancel_mandatory_1030(tmp_path):
    folder = tmp_path / "data/intraday-health"
    h.save(folder / "state.json", state(["09:30"], True, "2026-11-24T10:00:00+09:00"))
    result = h.run(
        tmp_path,
        clock=lambda: at("2026-11-24T10:00:00+09:00"),
        sleeper=lambda _: None,
        probe=Mock(side_effect=[healthy(1), healthy(2)]),
        starter=Mock(),
        session_check=lambda *_: True,
    )
    assert not result["next"]["needs_retry"]
    assert result["next"]["mandatory_checked"] == ["09:30"]
    assert result["next"]["next_mandatory"].endswith("10:30:00+09:00")


def test_no_progress_is_abnormal_even_with_recent_status(tmp_path):
    result = h.run(
        tmp_path,
        clock=at,
        sleeper=lambda _: None,
        probe=lambda *_: healthy(8),
        starter=Mock(),
        session_check=lambda *_: True,
    )
    assert result["state"] == "needs_attention"
    assert "panel:no_progress_during_verification" in result["after"]["issues"]


def test_holiday_performs_no_probe_or_repair(tmp_path):
    probe, starter = Mock(), Mock()
    result = h.run(tmp_path, clock=at, probe=probe, starter=starter, session_check=lambda *_: False)
    assert result["state"] == "market_closed"
    probe.assert_not_called()
    starter.assert_not_called()


def test_calendar_failure_records_retry_without_starting_apps(tmp_path):
    starter = Mock()
    result = h.run(
        tmp_path,
        clock=at,
        starter=starter,
        session_check=Mock(side_effect=ValueError("private calendar path")),
    )
    assert result["state"] == "needs_attention"
    assert result["next"]["needs_retry"]
    assert "private calendar path" not in json.dumps(result)
    starter.assert_not_called()


def test_manual_stop_and_unverified_lock_are_never_overridden():
    value = healthy()
    value["components"]["panel"].update(active=False, manual_stop=True)
    value["components"]["kabu_panel"].update(active=False, issues=["inspection_unavailable"])
    starter = Mock()
    assert h.recovery_actions(value, starter) == []
    starter.assert_not_called()


def test_failed_recovery_does_not_block_independent_broker():
    value = healthy()
    for name in ("panel", "kabu_panel"):
        value["components"][name].update(active=False)
    starter = Mock(side_effect=[OSError("private"), None])
    result = h.recovery_actions(value, starter)
    assert starter.call_count == 2
    assert [x["state"] for x in result] == ["recovery_failed", "worker_requested"]


def test_missing_database_allows_stopped_collector_start(tmp_path):
    snap = h.snapshot(tmp_path, at(), active=lambda _: False)
    assert "inspection_unavailable" not in snap["components"]["panel"]["issues"]
    starter = Mock()
    h.recovery_actions(snap, starter)
    assert {x.args[0] for x in starter.call_args_list} == set(h.TASKS)


def test_lunch_quote_age_and_future_old_date_validation():
    assert h.trading_age("2026-11-24T11:29:00+09:00", at("2026-11-24T12:31:00+09:00")) == 120
    assert h.trading_age("2026-11-23T15:00:00+09:00", at()) == float("inf")
    assert h.trading_age("2026-11-24T09:31:00+09:00", at()) == float("inf")


def test_recent_board_without_any_trade_is_not_an_outage(tmp_path):
    from trading_system.kabu_study import tables

    folder = tmp_path / "data/mufg-panel"
    folder.mkdir(parents=True)
    with closing(sqlite3.connect(folder / "research.sqlite")) as db:
        tables(db)
        for code in range(1000, 1031):
            raw = gzip.compress(
                json.dumps({"BidTime": at().isoformat(), "CurrentPrice": None}).encode()
            )
            db.execute(
                "INSERT INTO samples(known_at,quality,raw_board_gzip,symbol,operation) "
                "VALUES(?,?,?,?,?)",
                (at().isoformat(), "observed_book", raw, str(code), "board"),
            )
        db.commit()
    result = h.data_health(tmp_path, "kabu_panel", at() + timedelta(seconds=15))
    assert result["issues"] == [] and result["recent_symbols"] == 31


def test_rss_missing_untraded_symbol_and_temporary_partial_issue_are_not_full_outage(tmp_path):
    from trading_system.research_panel import initialize

    folder = tmp_path / "data/rss-panel"
    folder.mkdir(parents=True)
    raw = {
        "items": [
            {"symbol": "7203.T", "bars": [["2026/11/24", "09:29", 100, 101, 99, 100, 50]]},
            {"symbol": "4062.T", "bars": []},
        ],
        "chart_issue": {"reason": "excel_call_rejected"},
    }
    with closing(sqlite3.connect(folder / "research.sqlite")) as db:
        initialize(db)
        db.execute(
            "INSERT INTO frames(known_at,state,raw_gzip) VALUES(?,?,?)",
            (at().isoformat(), "read", gzip.compress(json.dumps(raw).encode())),
        )
        db.commit()
    result = h.data_health(tmp_path, "panel", at())
    assert result["issues"] == [] and result["recent_symbols"] == 1


def test_cooperative_signal_targets_only_older_processes_and_expires(tmp_path):
    moment = at()
    control.request_restart(tmp_path, moment)
    old = int((moment - timedelta(minutes=10)).timestamp() * 1e9)
    new = int((moment + timedelta(seconds=1)).timestamp() * 1e9)
    assert control.restart_requested(tmp_path, old, moment)
    assert not control.restart_requested(tmp_path, new, moment + timedelta(seconds=2))
    assert not control.restart_requested(tmp_path, old, moment + timedelta(minutes=2))
    assert not (tmp_path / "STOP").exists()


def test_corrupt_signal_is_ignored(tmp_path):
    (tmp_path / "health-restart.json").write_text("not-json")
    assert not control.restart_requested(tmp_path, 1, at())


def test_stale_collector_requests_cooperative_worker_but_login_wait_does_not():
    value = healthy()
    value["components"]["panel"].update(issues=["saved_observations_stale"])
    value["components"]["kabu_panel"].update(state="prompt", issues=["market_data_stale"])
    starter = Mock()
    h.recovery_actions(value, starter)
    starter.assert_called_once_with("panel")


def test_worker_never_overrides_manual_stop(tmp_path, monkeypatch):
    folder = tmp_path / "data/mufg-panel"
    folder.mkdir(parents=True)
    (folder / "STOP").write_text("operator")
    monkeypatch.setattr(h, "calendar_state", lambda *_: True)
    ensure = Mock()
    monkeypatch.setattr(worker.launcher, "ensure_broker_app", ensure)
    assert worker.run(tmp_path, "kabu_panel", clock=at) == "manual_stop"
    assert (folder / "STOP").read_text() == "operator"
    ensure.assert_not_called()


def test_worker_waits_for_locked_collector_without_forced_termination(tmp_path, monkeypatch):
    folder = tmp_path / "data/rss-panel"
    folder.mkdir(parents=True)
    h.save(folder / "status.json", {"state": "read"})
    monkeypatch.setattr(h, "calendar_state", lambda *_: True)
    monkeypatch.setattr(worker.launcher, "lock_active", lambda _: True)
    monkeypatch.setattr(worker.launcher, "ensure_broker_app", lambda *_: None)
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    value = healthy()
    value["components"]["panel"]["issues"] = ["saved_observations_stale"]
    monkeypatch.setattr(h, "snapshot", lambda *_: value)
    assert worker.run(tmp_path, "panel", clock=at) == "cooperative_restart_pending"
    assert (folder / "health-restart.json").exists()
    assert not (folder / "STOP").exists()


def test_worker_error_log_never_persists_exception_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "PROJECT", tmp_path)
    monkeypatch.setattr(worker, "run", Mock(side_effect=RuntimeError("SECRET-password")))
    assert worker.main(["--broker", "kabu_panel"]) == 2
    report = (tmp_path / "data/intraday-health/workers/kabu_panel/latest.json").read_text("utf-8")
    assert "RuntimeError" in report and "SECRET" not in report


def test_scheduler_worker_name_is_fixed_and_has_no_codex_dependency(monkeypatch):
    run = Mock()
    monkeypatch.setattr(h.subprocess, "run", run)
    h.start_task("panel")
    args = run.call_args.args[0]
    assert "Start-ScheduledTask" in args[-1]
    assert "Codex" not in str(args)
    with pytest.raises(ValueError):
        h.start_task("unapproved-process")


def test_rakuten_saves_current_frame_before_cooperative_restart(tmp_path, monkeypatch):
    from test_research_panel import CODES, T, sample

    from trading_system import research_panel

    root = tmp_path / "rss-panel"
    monkeypatch.setattr(research_panel, "configuration", lambda _: (CODES, root / "dummy.xlsx"))

    def read(*args, **kwargs):
        control.request_restart(root, T)
        return sample()

    research_panel.run(
        root,
        int((T - timedelta(minutes=1)).timestamp() * 1e9),
        "none",
        clock=lambda: T,
        sleep=lambda _: None,
        opener=lambda _: None,
        reader=read,
    )
    assert h.read_json(root / "status.json")["reason"] == "health_restart"
    with closing(sqlite3.connect(root / "research.sqlite")) as db:
        assert db.execute("SELECT count(*) FROM frames").fetchone() == (1,)
    assert not (root / "STOP").exists()


def test_kabu_saves_current_board_before_cooperative_restart(tmp_path):
    from test_research_panel import CODES, T

    from trading_system import kabu_study

    panel, root = tmp_path / "data/rss-panel", tmp_path / "data/mufg-panel"
    h.save(panel / "panel.json", {"version": 1, "symbols": CODES})

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def read(self, code):
            control.request_restart(root, T)
            record = {
                "operation": "board",
                "http_status": 200,
                "code": code,
                "outcome": "ok",
                "reason": None,
                "read_started_at": T.isoformat(),
                "read_completed_at": T.isoformat(),
                "retryable": False,
            }
            board = {
                "Symbol": code,
                "Exchange": 1,
                "Buy1": {"Price": 99, "Qty": 1},
                "Sell1": {"Price": 100, "Qty": 1},
            }
            return record, json.dumps(board).encode()

        def close(self):
            pass

    kabu_study.run(
        tmp_path,
        int((T - timedelta(minutes=1)).timestamp() * 1e9),
        "none",
        clock=lambda: T,
        sleep=lambda _: None,
        listener_ready=lambda: True,
        prompt=lambda *args, **kwargs: "test-only",
        client_factory=Client,
    )
    assert h.read_json(root / "status.json")["reason"] == "health_restart"
    with closing(sqlite3.connect(root / "research.sqlite")) as db:
        assert db.execute("SELECT count(*) FROM samples").fetchone() == (1,)
    assert not (root / "STOP").exists()


@pytest.mark.parametrize("broker", ["panel", "kabu_panel"])
def test_stopped_worker_starts_only_its_own_collector(tmp_path, monkeypatch, broker):
    from trading_system import kabu_study
    from trading_system.research import paired_start, shared_market

    monkeypatch.setattr(h, "calendar_state", lambda *_: True)
    monkeypatch.setattr(worker.launcher, "lock_active", lambda _: False)
    ensure = Mock()
    monkeypatch.setattr(worker.launcher, "ensure_broker_app", ensure)
    rss, kabu, finish = Mock(), Mock(), Mock()
    monkeypatch.setattr(paired_start, "main", rss)
    monkeypatch.setattr(kabu_study, "run", kabu)
    monkeypatch.setattr(shared_market, "after_stop", finish)
    assert worker.run(tmp_path, broker, clock=at) == "collector_completed"
    ensure.assert_called_once()
    if broker == "panel":
        rss.assert_called_once()
        kabu.assert_not_called()
        finish.assert_not_called()
    else:
        rss.assert_not_called()
        assert kabu.call_args.args[0] == tmp_path
        finish.assert_called_once_with(tmp_path, at().date(), "mufg_esmart")
