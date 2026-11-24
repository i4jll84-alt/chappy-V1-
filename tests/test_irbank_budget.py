import sqlite3
from contextlib import closing
from datetime import datetime
from unittest.mock import Mock

import httpx
import pytest

from trading_system.research import irbank_budget as budget
from trading_system.research import irbank_daily as daily
from trading_system.research import irbank_jsf as jsf
from trading_system.research import irbank_priority as priority


def record(root, day, count, kind="data"):
    with closing(sqlite3.connect(root / "requests.sqlite")) as db, db:
        db.executemany(
            "INSERT INTO requests(day,started,kind) VALUES(?,?,?)", [(day, "fixture", kind)] * count
        )


def setup(root):
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "requests.sqlite")) as db, db:
        db.execute("""CREATE TABLE requests(id INTEGER PRIMARY KEY,day TEXT,started TEXT,
                    kind TEXT,code TEXT,end_date TEXT,http_status INTEGER)""")
    record(root, "2026-11-22", 1298)
    record(root, "2026-11-23", 200)
    record(root, "2026-11-24", 200)
    record(root, "2026-11-24", 201, "usage")
    with closing(sqlite3.connect(root / "requests.sqlite")) as db:
        proof = budget.prefix_proof(db, 2, "2026-11-24")
    policy = {
        "starts_at": "2026-11-21T00:00:00+09:00",
        "ends_before": "2026-12-04T00:00:00+09:00",
        "initial_requests": 3,
        "initial_data_requests": 2,
        "max_total_requests": 10000,
        "quota_accounting": "data_only",
        "daily_schedule": {
            "from": "2026-11-24",
            "through": "2026-12-03",
            "max_data_requests": 1000,
        },
        "renewed_trial_budget": {
            "starts_on": "2026-11-24",
            "max_requests": 10000,
            "authorized_at": "fixture",
            "preserved_prior_ledger": proof,
        },
    }
    jsf.save(root / "acquisition-policy.json", policy)
    return policy


def stamp(text="2026-11-24T12:00:00+09:00"):
    return datetime.fromisoformat(text)


def test_prior_ledger_preserved_today_included_and_usage_excluded(tmp_path):
    policy = setup(tmp_path)
    with closing(sqlite3.connect(tmp_path / "requests.sqlite")) as db:
        totals = budget.data_totals(policy, db)
    assert totals == {"lifetime": 1700, "budget": 200, "prior": 1500, "start": "2026-11-24"}
    before = daily.inspect(tmp_path, stamp())
    assert before["remaining_trial_data"] == 9800
    assert before["remaining_daily_data"] == 800
    record(tmp_path, "2026-11-24", 800)
    after = daily.inspect(tmp_path, stamp())
    assert after["state"] == "daily_quota_reached"
    assert after["total_data_attempts"] == 2500 and after["trial_data_attempts"] == 1000
    assert after["remaining_trial_data"] == 9000
    with closing(sqlite3.connect(tmp_path / "requests.sqlite")) as db:
        assert (
            budget.prefix_proof(db, 2, "2026-11-24")
            == policy["renewed_trial_budget"]["preserved_prior_ledger"]
        )


def make_client(root, time):
    calls = []

    def serve(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"limit": 1000, "used": 200, "remaining": 800})

    return jsf.Client(
        root, "fixture", transport=httpx.MockTransport(serve), pause=Mock(), tick=lambda: time
    ), calls


@pytest.mark.parametrize(
    "reason", ["prior_tamper", "period_cap", "daily_cap", "expired", "before_renewal"]
)
def test_safety_gates_before_any_data_http(tmp_path, reason):
    policy = setup(tmp_path)
    time = stamp()
    expected = {
        "prior_tamper": "trial_budget_ledger_invalid",
        "period_cap": "trial_total_request_limit",
        "daily_cap": "authorized_daily_data_limit",
        "expired": "outside_free_trial_window",
        "before_renewal": "outside_free_trial_window",
    }[reason]
    if reason == "prior_tamper":
        with closing(sqlite3.connect(tmp_path / "requests.sqlite")) as db, db:
            db.execute("UPDATE requests SET http_status=503 WHERE id=1")
    elif reason == "period_cap":
        record(tmp_path, "2026-11-25", 9800)
    elif reason == "daily_cap":
        record(tmp_path, "2026-11-24", 800)
    elif reason == "expired":
        time = stamp("2026-12-04T00:00:00+09:00")
    elif reason == "before_renewal":
        time = stamp("2026-11-23T23:59:59+09:00")
    client, calls = make_client(tmp_path, time)
    client.usage_day = time.date()
    try:
        with pytest.raises(jsf.Halt, match=expected):
            client.send("data", "7203", "2021-11-19")
        assert calls == []
    finally:
        client.close()
    assert policy["renewed_trial_budget"]["preserved_prior_ledger"]["data_attempts"] == 1500


def test_last_day_inclusive_and_existing_legacy_policy_unchanged(tmp_path):
    policy = setup(tmp_path)
    assert daily.inspect(tmp_path, stamp("2026-12-03T23:59:59+09:00"))["state"] == "due"
    assert daily.inspect(tmp_path, stamp("2026-12-04T00:00:00+09:00"))["state"] == "expired"
    del policy["renewed_trial_budget"]
    with closing(sqlite3.connect(tmp_path / "requests.sqlite")) as db:
        assert budget.data_totals(policy, db)["budget"] == 1700


def test_required_phase_order_precedes_continuous_history(tmp_path, monkeypatch):
    root = tmp_path / "data/irbank_jsf"
    policy = setup(root)
    policy["priority_plan"] = {"mode": priority.MODE}
    jsf.save(root / "acquisition-policy.json", policy)
    monkeypatch.setattr(jsf, "now", stamp)
    monkeypatch.setattr(daily, "safe_check", lambda *a: {"state": "waiting"})
    phases = []

    def run(project, root, limit, phase):
        phases.append(phase)
        return {"state": "complete", "requests_this_run": 0}

    monkeypatch.setattr(jsf, "run", run)
    assert daily.run(tmp_path)["state"] == "archive_complete"
    assert phases == ["first5_required", "history_required", "first5", "history"]
