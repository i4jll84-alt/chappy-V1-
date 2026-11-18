import hashlib
import json
import sqlite3
from contextlib import nullcontext
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import duckdb
import pytest

from trading_system import plan_recovery as recovery
from trading_system.research import daytrade_universe


def local(day, t):
    return datetime.combine(date.fromisoformat(day), time.fromisoformat(t), recovery.JST)


def settings():
    return SimpleNamespace(
        data_root=recovery.Path("data/jquants"),
        model_copy=lambda **k: SimpleNamespace(**k["update"]),
    )


def meta(day, n=1):
    return dict(
        date=day,
        state="complete",
        daily_manifest="stub",
        daily_rows=n,
        completed_at=local(day, "18:00").isoformat(),
        jobs={k: {"state": "complete"} for k in ("calendar", "master", "topix", "financials")},
    )


def test_missing_and_partial_daily_only_are_recollected_with_real_receipt(tmp_path, monkeypatch):
    d = "2026-11-19"
    now = local("2026-11-20", "07:52")
    file = tmp_path / "data/jquants/daily-snapshots" / d / "last_collection.json"
    monkeypatch.setattr(recovery.Settings, "load", lambda *a: settings())
    observed = []

    def collect(day, s, *, run_paper):
        assert s.data_root == tmp_path / "data/jquants" and not run_paper
        observed.append(day)
        recovery.save(file, meta(d) | {"completed_at": now.isoformat()})

    monkeypatch.setattr(recovery.swing_daily, "collect", collect)
    monkeypatch.setattr(recovery, "dated_inputs", lambda *a: None)
    monkeypatch.setattr(recovery.a0_5_inputs, "market_pages", lambda *a: None)
    assert recovery.daily(tmp_path, d, lambda: now) == dict(
        status="recovered", available_at=now.isoformat()
    )
    assert recovery.daily(tmp_path, d, lambda: now)["status"] == "ready"
    partial = meta(d)
    partial["jobs"].pop("financials")
    recovery.save(file, partial)
    assert recovery.daily(tmp_path, d, lambda: now)["status"] == "recovered"
    monkeypatch.setattr(
        recovery,
        "dated_inputs",
        lambda *a: (_ for _ in ()).throw(ValueError("daily_hash_mismatch")),
    )
    with pytest.raises(ValueError, match="daily_hash_mismatch"):
        recovery.daily(tmp_path, d, lambda: now)
    assert len(observed) == 2


def test_multiple_missing_sessions_financial_bridge_and_holiday(tmp_path, monkeypatch):
    stamp = local("2026-11-24", "07:55")
    cfg = tmp_path / "config/forward-plans.json"
    recovery.save(cfg, {"plans": [{"stage": "active", "start": "2026-11-19"}]})
    archive = tmp_path / "data/jquants/history"
    recovery.save(archive / "quality-20261031.json", {"end": "2026-10-30"})
    with sqlite3.connect(archive / "history.sqlite") as db:
        db.execute("create table bulk_files(max_date,endpoint,state)")
        db.execute(
            "insert into bulk_files values (?,?,?)",
            ("2026-10-29", recovery.a0_5_inputs.SCHEMA["financial"][0], "complete"),
        )
    holiday = {"2026-11-03", "2026-11-23"}

    def exchange(jq, day, now):
        dates = [day + timedelta(days=i) for i in range(-30, 21)]
        return {
            str(d): "1" if d.weekday() < 5 and str(d) not in holiday else "0" for d in dates
        }, []

    monkeypatch.setattr(recovery, "calendar_days", exchange)
    root = tmp_path / "data/plan-forward/recovery"
    target, next_day, price, margin = recovery.coverage(tmp_path, stamp, root)
    assert (target, next_day) == ("2026-11-20", "2026-11-24")
    assert price[0] == "2026-10-30" and price[-2:] == ["2026-11-19", "2026-11-20"]
    assert margin[0] == "2026-10-29" and "2026-11-03" not in price
    recovery.save(root / "progress.json", dict(through=target))
    for d in price:
        recovery.save(
            tmp_path / "data/jquants/daily-snapshots" / d / "last_collection.json", meta(d)
        )
    assert recovery.coverage(tmp_path, stamp, root)[2] == [target]
    recovery.save(
        tmp_path / "data/jquants/daily-snapshots/2026-11-18/last_collection.json",
        {"date": "2026-11-18", "state": "partial"},
    )
    assert "2026-11-18" in recovery.coverage(tmp_path, stamp, root)[2]
    assert recovery.coverage(tmp_path, local("2026-11-23", "12:00"), root)[:2] == (target, next_day)
    assert recovery.coverage(tmp_path, local("2026-11-24", "18:15"), root)[0] == "2026-11-24"


def test_late_and_parallel_recovery_status_and_checkpoints(tmp_path, monkeypatch):
    now = local("2026-11-20", "09:15")
    root = tmp_path / "data/plan-forward/recovery"
    pending = {"failed": True}
    monkeypatch.setattr(
        recovery,
        "coverage",
        lambda *args: ("2026-11-19", "2026-11-20", ["2026-11-19"], ["2026-11-18", "2026-11-19"]),
    )

    def daily(*_):
        if pending["failed"]:
            raise ValueError("daily_not_yet_available")
        return recovery.result("ready")

    monkeypatch.setattr(recovery, "daily", daily)
    for name in ("jsf", "schedule_input", "margins", "flow", "evening_b"):
        monkeypatch.setattr(recovery, name, lambda *a: recovery.result("ready"))
    first = recovery.recover(tmp_path, now=now)
    assert first["status"] == "attention" and first["steps"]["earnings"]["status"] == "ready"
    assert first["steps"]["daily"]["reason"] == "daily_not_yet_available"
    assert not (root / "progress.json").is_file()
    pending["failed"] = False
    second = recovery.recover(tmp_path, now=now)
    assert second["status"] == "ok" and (root / "progress.json").is_file()
    with recovery.single_writer(root):
        assert recovery.recover(tmp_path, now=now) == {"status": "busy"}
    assert json.loads((root / "latest.json").read_bytes()) == second


def test_empty_weekly_margin_request_not_repeated_same_morning(tmp_path, monkeypatch):
    now = local("2026-11-20", "08:20")
    called = []
    monkeypatch.setattr(recovery.shorts, "capture_margin", lambda p, d: called.append(d) or "empty")
    root = tmp_path / "data/plan-forward/recovery"
    for _ in range(2):
        assert recovery.margins(tmp_path, "2026-11-19", lambda: now, root)["status"] == "empty"
    assert called == ["2026-11-19"]
    assert (
        recovery.margins(tmp_path, "2026-11-19", lambda: now + timedelta(days=1), root)["status"]
        == "empty"
    )
    assert len(called) == 2


@pytest.mark.parametrize(
    "hour,weekend,success,ran_a",
    [
        ("08:58", False, True, True),
        ("09:00", False, True, False),
        ("07:55", True, True, False),
        ("08:49", False, False, False),
    ],
)
def test_a_deadline_comes_before_slow_b_sources(
    tmp_path, monkeypatch, hour, weekend, success, ran_a
):
    target, next_day = "2026-11-19", "2026-11-20"
    when = local("2026-11-21" if weekend else next_day, hour)
    events = []
    monkeypatch.setattr(recovery, "coverage", lambda *_: (target, next_day, [target], [target]))
    monkeypatch.setattr(recovery, "daily", lambda *_: recovery.result("ready"))
    monkeypatch.setattr(
        recovery, "jsf", lambda *_: recovery.result("ready" if success else "error")
    )
    for name in ("schedule_input", "margins", "flow", "evening_b"):
        monkeypatch.setattr(
            recovery, name, lambda *_, name=name: events.append(name) or recovery.result("ready")
        )

    def early(actual_target, actual_next):
        assert (actual_target, actual_next) == (target, next_day)
        events.append("A")
        raise ValueError("independent_callback_failure")

    report = recovery.recover(tmp_path, now=when, on_a_inputs=early)
    assert ("A" in events) == ran_a
    if ran_a:
        assert events[0] == "A" and report["steps"]["a_preopen"]["status"] == "error"
    assert events[-1] == "evening_b" and report["steps"]["flow"]["status"] == "ready"


def test_supplemental_does_not_create_past_orders_or_a_late_panel(tmp_path, monkeypatch):
    d = "2026-11-19"
    now = local("2026-11-20", "09:01")
    root = tmp_path / "data/plan-forward/recovery"
    recovery.save(
        root / "latest.json", dict(status="ok", target=d, next_session="2026-11-20", steps={})
    )
    recovery.save(tmp_path / "config/daytrade-universe.json", dict(first_decision=d))
    monkeypatch.setattr(recovery, "public", lambda *a: recovery.result("ready"))
    monkeypatch.setattr(recovery.a0_5, "enabled", lambda p: True)
    calls = []
    monkeypatch.setattr(
        recovery.a0_5,
        "run",
        lambda p, day, *, now, capital: (
            calls.append((day, capital, now.date().isoformat())) or {"state": "reconciled"}
        ),
    )
    result = recovery.recover_supplemental(tmp_path, d, now=now)
    assert result["steps"]["intraday_panel"]["status"] == "missed"
    assert all(day < when for day, cap, when in calls)
    assert {cap for day, cap, when in calls} == set(recovery.a0_5.A0_5_PAPER_CAPITALS)
    assert result["steps"]["legacy_close"]["status"] == "skipped"


def test_evening_b_skips_prestart_and_recovers_missing_native_plan(tmp_path, monkeypatch):
    d = "2026-11-19"
    cfg = tmp_path / "config/forward-plans.json"
    monkeypatch.setattr(recovery.a0_5, "enabled", lambda p: True)
    monkeypatch.setattr(recovery.a0_5.book, "load", lambda *a: dict(plans={}))
    calls = []
    monkeypatch.setattr(
        recovery.a0_5, "run", lambda p, day, **kw: calls.append(day) or dict(state="ready")
    )
    plan = dict(stage="active", adapter="plan_b_forward", capital=3_000_000, start=d)
    recovery.save(cfg, dict(plans=[plan]))
    assert (
        recovery.evening_b(tmp_path, d, lambda: local("2026-11-20", "08:01"))["status"] == "skipped"
    )
    assert recovery.evening_b(tmp_path, d, lambda: local(d, "20:21"))["status"] == "recovered"
    assert calls == [d]


def test_preopen_checks_frozen_a_despite_independent_plan_outcome(tmp_path):
    d, n = "2026-11-19", "2026-11-20"
    now = local(n, "08:58")
    row = dict(
        start=d,
        next_entry=n,
        last_decision=d,
        decision_status="ready",
        status="waiting_close",
        decision_recorded_at=local(n, "08:42").isoformat(),
    )

    def assess(fields, overall="ok", checked=now):
        return recovery.preopen_result(
            tmp_path, d, n, dict(status=overall, plans={"A": row | fields}), checked
        )["status"]

    assert assess({}, "error") == "ready"
    assert (
        assess({"decision_status": None, "last_decision": None, "status": "waiting_inputs"})
        == "pending"
    )
    assert assess({"next_entry": d}) == "pending"
    assert assess({"decision_status": "stopped_preopen"}) == "stopped"
    assert assess({"start": n}) == "skipped"
    assert (
        assess({"decision_recorded_at": local(n, "09:00").isoformat()}, checked=local(n, "09:01"))
        == "missed"
    )
    assert assess({"decision_recorded_at": local(n, "08:59").isoformat()}) == "error"
    assert recovery.preopen_result(tmp_path, d, n, {"status": "busy"}, now)["status"] == "busy"


def test_recovery_defers_panel_while_regular_daily_still_advances_native_paper(
    tmp_path, monkeypatch
):
    daily = recovery.swing_daily
    when = date(2026, 11, 16)
    stamp = str(when)
    jq = tmp_path / "data/jquants"
    recovery.save(tmp_path / "config/daytrade-universe.json", {"first_decision": stamp})
    for key, fields in (("calendar", {"HolDiv": "1"}), ("master", {"Code": "72030"})):
        path = jq / "reference" / key / "raw.json"
        recovery.save(path, {"data": [dict(Date=stamp, **fields)]})
        with duckdb.connect(str(path.parent / "catalog.duckdb")) as db:
            db.execute("CREATE TABLE pages(raw_path VARCHAR,raw_sha256 VARCHAR,job_id VARCHAR)")
            db.execute(
                "INSERT INTO pages VALUES (?, ?, ?)",
                [path.name, hashlib.sha256(path.read_bytes()).hexdigest(), key],
            )
    monkeypatch.setattr(
        daily, "api_key", lambda: SimpleNamespace(get_secret_value=lambda: "fixture")
    )
    monkeypatch.setattr(daily, "JQuantsClient", lambda *a, **kwargs: nullcontext(None))

    def ingest(root, client, key, *args):
        return dict(job_id=key, state="complete", rows_received=1)

    monkeypatch.setattr(daily, "ingest_reference", ingest)
    monkeypatch.setattr(daily, "ingest_market_financial", ingest)
    manifest = jq / "daily-snapshots" / stamp / "test/manifest.json"
    recovery.save(manifest, {"pages": []})
    monkeypatch.setattr(daily, "snapshot", lambda *args, **kwargs: (manifest, 1))
    events = []
    monkeypatch.setattr(daily, "wait_for_daily_pages", lambda p: events.append("available"))
    monkeypatch.setattr(
        daytrade_universe, "prepare", lambda *args: events.append("panel") or manifest.parent
    )
    monkeypatch.setattr(recovery.a0_5, "after_daily", lambda *args: events.append("native"))
    configuration = SimpleNamespace(plan="fixture", data_root=jq)
    daily.collect(when, configuration, run_paper=False)
    assert events == ["available"]
    daily.collect(when, configuration)
    assert events == ["available", "available", "panel", "native"]
