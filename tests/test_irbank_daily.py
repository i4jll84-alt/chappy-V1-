import json
import sqlite3
from contextlib import closing
from datetime import datetime
from unittest.mock import Mock

import httpx
import pytest

from trading_system.research import irbank_daily as daily
from trading_system.research import irbank_jsf as jsf


def prepare(tmp_path, total=10000):
    root = tmp_path / "data/irbank_jsf"
    root.mkdir(parents=True)
    jsf.save(
        root / "acquisition-policy.json",
        {
            "starts_at": "2026-11-21T00:00:00+09:00",
            "ends_before": "2026-12-01T22:30:00+09:00",
            "initial_requests": 3,
            "initial_data_requests": 2,
            "max_total_requests": total,
            "quota_accounting": "data_only",
            "daily_schedule": {
                "from": "2026-11-23",
                "through": "2026-12-01",
                "max_data_requests": 1000,
            },
        },
    )
    with closing(sqlite3.connect(root / "requests.sqlite")) as db, db:
        db.execute("""CREATE TABLE requests(
            id INTEGER PRIMARY KEY,day TEXT,started TEXT,kind TEXT,
            code TEXT,end_date TEXT,http_status INTEGER)""")
    return root


def record(root, day, count, kind="data"):
    with closing(sqlite3.connect(root / "requests.sqlite")) as db, db:
        db.executemany(
            "INSERT INTO requests(day,started,kind) VALUES(?,?,?)", [(day, "fixture", kind)] * count
        )


def stamp(value="2026-11-23T21:00:00+09:00"):
    return datetime.fromisoformat(value)


def test_daily_count_excludes_usage_and_preserves_initial_probe(tmp_path):
    root = prepare(tmp_path)
    record(root, "2026-11-23", 900)
    record(root, "2026-11-23", 1500, "usage")
    r = daily.inspect(root, stamp())
    assert r["daily_data_attempts"] == 900 and r["total_data_attempts"] == 902
    assert r["remaining_daily_data"] == 100 and r["state"] == "due"


@pytest.mark.parametrize(
    ("time", "state"),
    [
        ("2026-11-22T21:00:00+09:00", "not_started"),
        ("2026-12-01T22:29:59+09:00", "due"),
        ("2026-12-01T22:30:00+09:00", "expired"),
        ("2026-12-02T21:00:00+09:00", "expired"),
    ],
)
def test_dates_and_trial_deadline(tmp_path, time, state):
    root = prepare(tmp_path)
    assert daily.inspect(root, stamp(time))["state"] == state


def test_inspect_and_before_start_do_not_fetch_or_load_credentials(tmp_path, monkeypatch):
    root = prepare(tmp_path)
    monkeypatch.setattr(jsf, "now", lambda: stamp("2026-11-22T21:00:00+09:00"))
    blocked = Mock(side_effect=AssertionError("network or secret access"))
    monkeypatch.setattr(jsf, "run", blocked)
    monkeypatch.setattr(jsf, "load_key", blocked)
    assert daily.run(tmp_path)["state"] == "not_started"
    assert daily.main(["--project", str(tmp_path), "--inspect"]) == 0
    assert not (root / "daily").exists()
    blocked.assert_not_called()


def test_missing_request_ledger_never_resets_quota(tmp_path):
    root = prepare(tmp_path)
    (root / "requests.sqlite").unlink()
    with pytest.raises(sqlite3.OperationalError):
        daily.inspect(root, stamp())
    assert not (root / "requests.sqlite").exists()


def test_wrapper_resumes_three_bounded_batches_to_daily_1000(tmp_path, monkeypatch):
    root = prepare(tmp_path)
    monkeypatch.setattr(jsf, "now", stamp)
    calls = []

    def batch(project, where, limit, phase):
        calls.append((limit, phase))
        n = min(475, daily.inspect(where)["remaining_daily_data"])
        record(where, "2026-11-23", n)
        return {
            "state": "stopped",
            "reason": "run_request_limit" if n == 475 else "remote_daily_headroom",
            "requests_this_run": 2 * n,
            "complete": 1,
        }

    monkeypatch.setattr(jsf, "run", batch)
    result = daily.run(tmp_path)
    assert result["state"] == "daily_quota_reached"
    assert result["data_attempts_this_execution"] == 1000
    assert calls == [(950, "first5")] * 3
    assert (root / "daily/2026-11-23/latest.json").exists()
    assert daily.run(tmp_path)["state"] == "daily_quota_reached"
    assert len(calls) == 3


def test_history_only_after_first5_completes(tmp_path, monkeypatch):
    prepare(tmp_path)
    monkeypatch.setattr(jsf, "now", stamp)
    phases = []

    def batch(project, root, limit, phase):
        phases.append(phase)
        return {"state": "complete", "requests_this_run": 0}

    monkeypatch.setattr(jsf, "run", batch)
    assert daily.run(tmp_path)["state"] == "archive_complete"
    assert phases == ["first5", "history"]


def test_error_is_not_retried_as_a_fresh_daily_allowance(tmp_path, monkeypatch):
    prepare(tmp_path)
    monkeypatch.setattr(jsf, "now", stamp)
    blocked = Mock(
        return_value={"state": "stopped", "reason": "repeated_429", "requests_this_run": 4}
    )
    monkeypatch.setattr(jsf, "run", blocked)
    result = daily.run(tmp_path)
    assert result["state"] == "needs_attention" and result["reason"] == "repeated_429"
    blocked.assert_called_once()


def test_trial_total_stops_on_data_attempts_even_if_usage_succeeds(tmp_path):
    root = prepare(tmp_path, total=4)
    record(root, "2026-11-22", 2)
    record(root, "2026-11-22", 50, "usage")
    calls = []

    def serve(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"limit": 1000, "used": 0, "remaining": 1000})

    c = jsf.Client(root, "fixture", transport=httpx.MockTransport(serve), pause=Mock(), tick=stamp)
    try:
        with pytest.raises(jsf.Halt, match="trial_total_request_limit"):
            c.get("7203", "2021-11-19")
        assert calls == ["/v1/usage"]
        assert daily.inspect(root, stamp())["state"] == "trial_budget_exhausted"
    finally:
        c.close()


def test_client_refuses_new_day_and_expired_trial_before_network(tmp_path):
    root = prepare(tmp_path)
    transport = Mock(side_effect=AssertionError("unexpected network"))
    c = jsf.Client(
        root, "fixture", transport=httpx.MockTransport(transport), pause=Mock(), tick=stamp
    )
    try:
        c.tick = lambda: stamp("2026-11-24T00:00:00+09:00")
        with pytest.raises(jsf.Halt, match="day_changed_resume"):
            c.refresh_usage()
        c.tick = lambda: stamp("2026-12-01T22:30:00+09:00")
        with pytest.raises(jsf.Halt, match="outside_free_trial_window"):
            c.refresh_usage()
        transport.assert_not_called()
    finally:
        c.close()


def test_second_daily_dispatch_is_blocked(tmp_path, monkeypatch):
    root = prepare(tmp_path)
    monkeypatch.setattr(jsf, "now", stamp)
    blocked = Mock(side_effect=AssertionError("second worker started"))
    monkeypatch.setattr(jsf, "run", blocked)
    with jsf.single_writer(root / "daily-dispatch"):
        with pytest.raises(ValueError, match="broker_state_already_open"):
            daily.run(tmp_path)
    blocked.assert_not_called()


def test_daily_schedule_cannot_raise_standard_cap(tmp_path):
    root = prepare(tmp_path)
    p = root / "acquisition-policy.json"
    policy = json.loads(p.read_bytes())
    policy["daily_schedule"]["max_data_requests"] = 1001
    jsf.save(p, policy)
    with pytest.raises(jsf.Halt, match="invalid_daily_policy"):
        daily.inspect(root, stamp())
    c = jsf.Client(root, "fixture", transport=httpx.MockTransport(Mock()), pause=Mock(), tick=stamp)
    try:
        with pytest.raises(jsf.Halt, match="invalid_daily_schedule"):
            c.get("7203", "2021-11-19")
    finally:
        c.close()


@pytest.mark.parametrize("limit", [200, 1000])
def test_scheduled_client_uses_last_slot_and_usage_never_consumes_data_quota(tmp_path, limit):
    root = prepare(tmp_path)
    policy_path = root / "acquisition-policy.json"
    policy = json.loads(policy_path.read_bytes())
    policy["daily_schedule"]["max_data_requests"] = limit
    jsf.save(policy_path, policy)
    record(root, "2026-11-23", limit - 1)
    record(root, "2026-11-23", 1200, "usage")
    used = [limit - 1]

    def serve(request):
        if request.url.path.endswith("/usage"):
            return httpx.Response(
                200, json={"limit": limit, "used": used[0], "remaining": limit - used[0]}
            )
        used[0] += 1
        return httpx.Response(200, json={})

    c = jsf.Client(root, "fixture", transport=httpx.MockTransport(serve), pause=Mock(), tick=stamp)
    try:
        assert c.get("7203", "2021-11-19").status_code == 200
        with pytest.raises(jsf.Halt, match="remote_daily_headroom"):
            c.get("7203", "2021-11-19")
        assert used[0] == limit and c.sent == 3
        status = daily.inspect(root, stamp())
        assert status["daily_data_attempts"] == status["daily_data_limit"] == limit
        assert status["state"] == "daily_quota_reached"
    finally:
        c.close()
