import json
from datetime import datetime
from unittest.mock import Mock

import httpx
import pytest

from trading_system.research import irbank_jsf as m


def setup(root, *, limit=9500, initial=3):
    m.save(
        root / "acquisition-policy.json",
        {
            "starts_at": "2026-11-21T00:00:00+09:00",
            "ends_before": "2026-12-01T22:30:00+09:00",
            "max_total_requests": limit,
            "initial_requests": initial,
        },
    )


def body(days=("2023-10-30", "2023-10-31")):
    return {
        "security_code": "7203",
        "period": "1y",
        "exchange": "XTKS",
        "fields": [{"key": k, "unit": "shares"} for k in m.BALANCES],
        "days": [
            dict(
                date=d,
                loan_balance=100,
                stock_loan_balance=80,
                net_balance=20,
                loan_new=1,
                loan_repay=2,
                stock_loan_new=3,
                stock_loan_repay=4,
            )
            for d in days
        ],
    }


def client(root, statuses=None, *, limit=950, stamp="2026-11-21T23:00:00+09:00"):
    calls = []
    codes = list(statuses or [200])

    def serve(request):
        assert request.headers["Authorization"] == "Bearer fixture-secret"
        calls.append(request.url.path)
        if request.url.path.endswith("/usage"):
            return httpx.Response(200, json={"limit": 1000, "used": 0, "remaining": 1000})
        status = codes.pop(0) if codes else 200
        return httpx.Response(status, json=body())

    c = m.Client(
        root,
        "fixture-secret",
        limit,
        transport=httpx.MockTransport(serve),
        pause=Mock(),
        tick=lambda: datetime.fromisoformat(stamp),
    )
    return c, calls


def test_multi_page_resume_and_404_are_cached_without_repeating_requests(tmp_path):
    setup(tmp_path)
    archive = m.Archive(tmp_path)
    c, calls = client(tmp_path, [200, 404])
    try:
        state = m.work_window(archive, c, "history", "7203", "2016-11-17", "2023-10-31")
        assert state["state"] == "complete" and state["empty_pages"] == 1
        assert c.sent == 3  # one usage call and two data attempts
        before = list(calls)
        assert m.work_window(archive, c, "history", "7203", "2016-11-17", "2023-10-31") == state
        assert calls == before
        data = json.loads((tmp_path / "7203.json").read_bytes())
        assert set(data["days"]) == {"2023-10-30", "2023-10-31"}
    finally:
        c.close()


def test_retry_attempts_are_reserved_and_run_limit_cannot_be_bypassed(tmp_path):
    setup(tmp_path)
    c, calls = client(tmp_path, [503, 503, 200], limit=3)
    try:
        with pytest.raises(m.Halt, match="run_request_limit"):
            c.get("7203", "2023-10-31")
        assert c.sent == 3 and len(calls) == 3
        assert c.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 3
        assert c.pause.call_count >= 2
    finally:
        c.close()


def test_repeated_429_stops_without_a_third_data_attempt(tmp_path):
    setup(tmp_path)
    c, calls = client(tmp_path, [429, 429, 200])
    try:
        with pytest.raises(m.Halt, match="repeated_429"):
            c.get("7203", "2023-10-31")
        assert len(calls) == 3
    finally:
        c.close()


@pytest.mark.parametrize("reason", ["daily", "total", "expired"])
def test_limits_survive_relaunch_and_expiry_sends_nothing(tmp_path, reason):
    setup(tmp_path, limit=3 if reason == "total" else 9500)
    c, calls = client(
        tmp_path,
        stamp="2026-12-01T22:30:00+09:00" if reason == "expired" else "2026-11-21T23:00:00+09:00",
    )
    if reason == "daily":
        c.db.executemany(
            "INSERT INTO requests(day,started,kind) VALUES(?,?,?)",
            [("2026-11-21", "fixture", "data")] * 950,
        )
        c.db.commit()
    try:
        with pytest.raises(m.Halt):
            c.get("7203", "2023-10-31")
        assert not calls
    finally:
        c.close()


def test_remote_shared_usage_preserves_fifty_request_margin(tmp_path):
    setup(tmp_path)
    seen = []

    def serve(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"limit": 1000, "used": 950, "remaining": 50})

    c = m.Client(
        tmp_path,
        "secret",
        transport=httpx.MockTransport(serve),
        pause=Mock(),
        tick=lambda: datetime.fromisoformat("2026-11-21T23:00:00+09:00"),
    )
    try:
        with pytest.raises(m.Halt, match="remote_daily_headroom"):
            c.get("7203", "2023-10-31")
        assert seen == ["/v1/usage"]
    finally:
        c.close()


def test_cache_corruption_and_overlapping_corrections_are_not_silently_replaced(tmp_path):
    setup(tmp_path)
    archive = m.Archive(tmp_path)
    receipt, rows = archive.write("7203", "2023-10-31", httpx.Response(200, json=body()))
    archive.merge("7203", receipt, rows)
    with pytest.raises(m.Halt, match="overlapping_pages_conflict"):
        archive.merge("7203", receipt, [dict(rows[0], loan_balance=101, net_balance=21)])
    raw, _ = archive.paths("7203", "2023-10-31")
    raw.write_text("{}")
    with pytest.raises(m.Halt, match="raw_hash_mismatch"):
        archive.read("7203", "2023-10-31")


def test_day_rollover_requires_a_fresh_quota_check(tmp_path):
    setup(tmp_path)
    c, calls = client(tmp_path)
    try:
        c.refresh_usage()
        c.tick = lambda: datetime.fromisoformat("2026-11-22T00:00:00+09:00")
        with pytest.raises(m.Halt, match="day_changed_resume"):
            c.send("data", "7203", "2023-10-31")
        assert len(calls) == 1
    finally:
        c.close()


def test_dotenv_and_environment_keys_are_read_without_persisting_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("IRBANK_API_KEY", raising=False)
    (tmp_path / ".env").write_text('IRBANK_API_KEY="fixture-secret"')
    assert m.load_key(tmp_path) == "fixture-secret"
    monkeypatch.setenv("IRBANK_API_KEY", "environment-secret")
    assert m.load_key(tmp_path) == "environment-secret"


def test_initial_probe_attempts_also_count_toward_first_day_limit(tmp_path):
    setup(tmp_path)
    c, calls = client(tmp_path)
    c.db.executemany(
        "INSERT INTO requests(day,started,kind) VALUES(?,?,?)",
        [("2026-11-21", "fixture", "data")] * 947,
    )
    c.db.commit()
    try:
        with pytest.raises(m.Halt, match="local_daily_request_limit"):
            c.get("7203", "2023-10-31")
        assert not calls
    finally:
        c.close()


def test_overlapping_archive_windows_reuse_raw_pages_without_api_calls(tmp_path):
    archive = m.Archive(tmp_path)
    archive.write("7203", "2023-11-01", httpx.Response(200, json=body()))
    blocked = Mock()
    blocked.get.side_effect = AssertionError("unexpected network request")
    state = m.work_window(archive, blocked, "history", "7203", "2023-10-30", "2023-10-31")
    assert state["state"] == "complete"
    assert state["pages"] == ["2023-11-01"]
    blocked.get.assert_not_called()


def test_pipeline_cannot_call_first_five_years_the_complete_ten_year_study(tmp_path, monkeypatch):
    from trading_system.research import irbank_audit, irbank_replay, irbank_required

    monkeypatch.setattr(m, "run", lambda *args: {"state": "complete", "requests_this_run": 0})
    monkeypatch.setattr(irbank_audit, "run", lambda *args: {"state": "checked"})
    monkeypatch.setattr(
        irbank_required, "run_needed", lambda *args: {"state": "complete", "requests_this_run": 0}
    )
    monkeypatch.setattr(
        irbank_replay, "run", lambda *args: {"state": "complete", "scope": "first5"}
    )
    result = m.pipeline(tmp_path, tmp_path)
    assert result["state"] == "waiting"
    assert result["replay"]["scope"] == "first5"


def test_pipeline_stops_before_any_replay_if_original_balance_definition_does_not_match(
    tmp_path, monkeypatch
):
    from trading_system.research import irbank_audit, irbank_replay, irbank_required

    monkeypatch.setattr(m, "run", lambda *args: {"state": "complete", "requests_this_run": 0})
    monkeypatch.setattr(irbank_audit, "run", lambda *args: {"state": "needs_review"})
    blocked = Mock(side_effect=AssertionError("audit gate bypassed"))
    monkeypatch.setattr(irbank_required, "run_needed", blocked)
    monkeypatch.setattr(irbank_replay, "run", blocked)
    result = m.pipeline(tmp_path, tmp_path)
    assert result["state"] == "needs_review"
    blocked.assert_not_called()


def test_ambiguous_404_does_not_claim_other_windows_are_observed(tmp_path):
    archive = m.Archive(tmp_path)
    archive.write("7203", "2023-11-01", httpx.Response(404, json={"error": "not_found"}))
    assert archive.read("7203", "2023-11-01")[1] == []
    assert archive.covering("7203", "2023-10-31") is None


def test_first5_prioritizes_frozen_candidates_without_excluding_boundary_inputs(
    tmp_path, monkeypatch
):
    from trading_system.research import irbank_required, irbank_universe

    universe = {
        "codes": ["7203", "1111", "2222"],
        "boundary_only_codes": ["3333"],
        "history_start": "2016-11-17",
    }
    technical = {
        "days": {
            "2016-11-18": [{"code": "72030"}, {"code": "99990"}],
            "2021-11-18": [{"code": "11110"}, {"code": "72030"}, {"code": "33330"}],
            "2021-11-19": [{"code": "22220"}],
        }
    }
    monkeypatch.setattr(irbank_universe, "prepare", lambda *args: universe)
    monkeypatch.setattr(irbank_required, "prepare_candidates", lambda *args: technical)
    monkeypatch.setattr(m, "load_key", lambda *args: "fixture-only")
    fake_client = Mock(sent=0)
    constructor = Mock(return_value=fake_client)
    monkeypatch.setattr(m, "Client", constructor)
    jobs = []

    def collect(archive, client, *job):
        jobs.append(job)
        return {"empty_pages": 0}

    monkeypatch.setattr(m, "work_window", collect)
    report = m.run(tmp_path, tmp_path, max_requests=17, phase="first5")
    assert jobs == [
        ("first5", code, "2016-11-17", "2021-11-19") for code in ["7203", "1111", "3333"]
    ]
    assert report["jobs"] == report["complete"] == 3
    assert report["remaining"] == report["requests_this_run"] == 0
    assert report["state"] == "complete"
    constructor.assert_called_once_with(tmp_path, "fixture-only", 17)
    fake_client.close.assert_called_once()
