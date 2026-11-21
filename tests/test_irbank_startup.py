import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from trading_system.research import irbank_daily as daily
from trading_system.research import irbank_jsf as jsf
from trading_system.research import irbank_startup as startup


def at(value="2026-11-24T08:00:00+09:00"):
    return datetime.fromisoformat(value)


def prepare(project):
    root = project / "data/irbank_jsf"
    root.mkdir(parents=True)
    jsf.save(
        root / "acquisition-policy.json",
        {
            "starts_at": "2026-11-21T00:00:00+09:00",
            "ends_before": "2026-12-01T22:30:00+09:00",
            "initial_requests": 3,
            "initial_data_requests": 2,
            "max_total_requests": 10000,
            "quota_accounting": "data_only",
            "daily_schedule": {
                "from": "2026-11-23",
                "through": "2026-12-01",
                "max_data_requests": 1000,
                "time_jst": "21:00",
            },
        },
    )
    with closing(sqlite3.connect(root / "requests.sqlite")) as db, db:
        db.execute("""CREATE TABLE requests(
            id INTEGER PRIMARY KEY,day TEXT,started TEXT,kind TEXT,
            code TEXT,end_date TEXT,http_status INTEGER)""")
    automation = project / "automation.toml"
    automation.write_text('id = "irbank-5"\nstatus = "ACTIVE"\n', encoding="utf-8")
    return root, automation


def record(root, day, count):
    with closing(sqlite3.connect(root / "requests.sqlite")) as db, db:
        db.executemany(
            "INSERT INTO requests(day,started,kind) VALUES(?,?,?)",
            [(day, "fixture", "data")] * count,
        )


@pytest.mark.parametrize(
    ("moment", "state"),
    [
        ("2026-11-22T23:00:00+09:00", "not_started"),
        ("2026-11-23T08:00:00+09:00", "waiting_for_first_schedule"),
        ("2026-11-23T20:59:59+09:00", "waiting_for_first_schedule"),
        ("2026-12-01T22:30:00+09:00", "expired"),
        ("2026-12-02T08:00:00+09:00", "expired"),
    ],
)
def test_before_first_schedule_and_expiry_send_nothing(tmp_path, moment, state):
    _, automation = prepare(tmp_path)
    blocked = Mock(side_effect=AssertionError("unexpected acquisition"))
    result = startup.check(
        tmp_path,
        automation,
        at(moment) - timedelta(minutes=1),
        1,
        moment=at(moment),
        runner=blocked,
    )
    assert result["state"] == state
    blocked.assert_not_called()


def test_missed_evening_resumes_next_morning_using_only_todays_remaining_quota(tmp_path):
    root, automation = prepare(tmp_path)
    record(root, "2026-11-23", 150)
    record(root, "2026-11-24", 200)
    before = daily.inspect(root, at())

    def runner(project):
        remaining = daily.inspect(root, at())["remaining_daily_data"]
        assert remaining == 800
        record(root, "2026-11-24", remaining)
        return {"state": "daily_quota_reached"}

    result = startup.check(
        tmp_path, automation, at() - timedelta(seconds=30), 1, moment=at(), runner=runner
    )
    assert result["due_at"] == "2026-11-23T21:00:00+09:00"
    assert result["state"] == "finished"
    after = daily.inspect(root, at())
    assert after["daily_data_attempts"] == 1000
    assert after["total_data_attempts"] - before["total_data_attempts"] == 800
    blocked = Mock(side_effect=AssertionError("quota duplicated"))
    startup.check(tmp_path, automation, at(), 2, moment=at() + timedelta(minutes=1), runner=blocked)
    blocked.assert_not_called()


def test_successful_previous_night_does_not_fetch_again_next_morning(tmp_path):
    root, automation = prepare(tmp_path)
    record(root, "2026-11-23", 1000)
    blocked = Mock(side_effect=AssertionError("unexpected acquisition"))
    assert (
        startup.check(tmp_path, automation, at(), 1, moment=at(), runner=blocked)["state"]
        == "scheduled_quota_already_met"
    )
    blocked.assert_not_called()


def test_remote_quota_receipt_counts_only_for_matching_scheduled_day(tmp_path):
    root, automation = prepare(tmp_path)
    receipt = root / "daily/2026-11-23/latest.json"
    saved = {
        "state": "daily_quota_reached",
        "after": {"date": "2026-11-23"},
        "last_remote_usage": {
            "checked_at": "2026-11-23T22:00:00+09:00",
            "used": 1000,
            "limit": 1000,
            "remaining": 0,
        },
    }
    jsf.save(receipt, saved)
    assert startup.assess(tmp_path, automation, at())["state"] == "scheduled_quota_already_met"
    saved["last_remote_usage"]["checked_at"] = "2026-11-22T22:00:00+09:00"
    jsf.save(receipt, saved)
    assert startup.assess(tmp_path, automation, at())["state"] == "due"


def test_failed_start_is_not_retried_every_minute_but_new_app_start_can_resume(tmp_path):
    _, automation = prepare(tmp_path)
    runner = Mock(return_value={"state": "needs_attention", "reason": "retry_exhausted"})
    first = startup.check(tmp_path, automation, at(), 1, moment=at(), runner=runner)
    assert first["state"] == "failed"
    second = startup.check(
        tmp_path, automation, at(), 1, moment=at() + timedelta(minutes=1), runner=runner
    )
    assert second["state"] == "startup_already_handled" and runner.call_count == 1
    startup.check(
        tmp_path,
        automation,
        at() + timedelta(minutes=2),
        2,
        moment=at() + timedelta(minutes=2),
        runner=runner,
    )
    assert runner.call_count == 2


def test_interrupted_run_can_resume_same_app_session(tmp_path):
    root, automation = prepare(tmp_path)
    interrupted = Mock(side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        startup.check(tmp_path, automation, at(), 1, moment=at(), runner=interrupted)
    assert json.loads((root / "startup/status.json").read_bytes())["state"] == "running"
    runner = Mock(return_value={"state": "daily_quota_reached"})
    assert (
        startup.check(tmp_path, automation, at(), 1, moment=at(), runner=runner)["state"]
        == "finished"
    )
    runner.assert_called_once()


def test_day_change_is_resumable_not_marked_complete(tmp_path):
    _, automation = prepare(tmp_path)
    runner = Mock(return_value={"state": "day_changed"})
    assert (
        startup.check(tmp_path, automation, at(), 1, moment=at(), runner=runner)["state"]
        == "interrupted"
    )
    runner.return_value = {"state": "daily_quota_reached"}
    assert (
        startup.check(tmp_path, automation, at(), 1, moment=at(), runner=runner)["state"]
        == "finished"
    )
    assert runner.call_count == 2


def test_concurrent_evening_run_is_not_an_error_or_second_fetch(tmp_path, monkeypatch):
    root, automation = prepare(tmp_path)
    monkeypatch.setattr(jsf, "now", at)
    with jsf.single_writer(root / "daily-dispatch"):
        result = startup.check(tmp_path, automation, at(), 1, moment=at())
        assert result["state"] == "already_running"
        assert daily.main(["--project", str(tmp_path)]) == 0
    assert daily.inspect(root, at())["daily_data_attempts"] == 0


@pytest.mark.parametrize("mode", ["paused", "deleted"])
def test_paused_or_deleted_automation_disables_startup_fetch(tmp_path, mode):
    _, automation = prepare(tmp_path)
    if mode == "paused":
        automation.write_text('id="irbank-5"\nstatus="PAUSED"\n', encoding="utf-8")
    else:
        automation.unlink()
    blocked = Mock(side_effect=AssertionError("unexpected acquisition"))
    assert (
        startup.check(tmp_path, automation, at(), 1, moment=at(), runner=blocked)["state"]
        == "automation_disabled"
    )
    blocked.assert_not_called()


def test_trial_total_also_blocks_catchup(tmp_path):
    root, automation = prepare(tmp_path)
    record(root, "2026-11-23", 9998)
    blocked = Mock(side_effect=AssertionError("unexpected acquisition"))
    assert (
        startup.check(tmp_path, automation, at(), 1, moment=at(), runner=blocked)["state"]
        == "trial_budget_exhausted"
    )
    blocked.assert_not_called()


def test_invalid_start_metadata_is_rejected(tmp_path):
    _, automation = prepare(tmp_path)
    for started, pid in [
        (at().replace(tzinfo=None), 1),
        (at() + timedelta(seconds=1), 1),
        (at(), 0),
    ]:
        with pytest.raises(ValueError, match="invalid_app_start"):
            startup.check(tmp_path, automation, started, pid, moment=at())


def test_no_desktop_process_never_starts_acquisition(tmp_path, monkeypatch):
    _, automation = prepare(tmp_path)
    monkeypatch.setattr(startup, "detect_app", lambda: None)
    blocked = Mock(side_effect=AssertionError("unexpected acquisition"))
    monkeypatch.setattr(startup, "check", blocked)
    assert (
        startup.main(["--project", str(tmp_path), "--automation", str(automation), "--detect-app"])
        == 0
    )
    blocked.assert_not_called()


def test_native_process_query_parses_utc_and_handles_no_app(monkeypatch):
    from types import SimpleNamespace

    reply = Mock(
        return_value=SimpleNamespace(stdout='{"pid":123,"started":"2026-11-23T23:00:00.1234567Z"}')
    )
    monkeypatch.setattr(startup.subprocess, "run", reply)
    started, pid = startup.detect_app()
    assert started == at("2026-11-24T08:00:00.123456+09:00") and pid == 123
    reply.return_value = SimpleNamespace(stdout="")
    assert startup.detect_app() is None
