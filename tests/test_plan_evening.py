import hashlib
import sqlite3
from datetime import date, datetime, time

import pytest

from trading_system import plan_evening as evening
from trading_system import plan_recovery as recovery

DAY = "2026-11-19"
PREVIOUS = "2026-11-18"


def local(t, d=DAY):
    return datetime.combine(date.fromisoformat(d), time.fromisoformat(t), recovery.JST)


def observations(target):
    rows = {PREVIOUS: {"status": "ready"}, target: {"status": "recovered"}}
    steps = {
        "jsf": {"status": "recovered", "dates": rows},
        "margin": {"status": "recovered", "dates": rows.copy()},
        "earnings": {
            "status": "ready",
            "manifest": "local/manifest.json",
            "companies": 2,
            "files": 1,
        },
        "short_public": {"status": "ready"},
    }
    result = {"status": "ok", "target": target, "steps": steps}
    return {
        "status": "ok",
        "recovery": result,
        "supplemental": result,
        "plan_forward": {"status": "ok", "plans": {}},
    }


def set_clock(monkeypatch, flag):
    monkeypatch.setattr(
        evening, "calendar_days", lambda root, day, now: ({day.isoformat(): flag}, [])
    )


def test_due_receipt_with_shared_sources_and_no_retired_accounts(tmp_path, monkeypatch):
    set_clock(monkeypatch, "1")
    calls = []
    monkeypatch.setattr(evening, "collect", lambda p, n: calls.append((p, n)) or observations(DAY))
    assert evening.run_evening(tmp_path, now=local("21:59"))["state"] == "not_due"
    assert not (tmp_path / "data").exists()
    r = evening.run_evening(tmp_path, now=local("22:00"))
    assert r["status"] == "ok" and r["state"] == "evening"
    assert r["short_margin"] == {PREVIOUS: "already_acquired", DAY: "acquired"}
    assert r["credit"] == r["short_margin"] and r["short_public"] == "already_acquired"
    assert r["earnings"]["companies"] == 2
    assert "settled" not in r and "review" not in r
    path = tmp_path / "data/plan-forward/recovery/evening" / (DAY + ".json")
    old = path.read_bytes()
    again = evening.run_evening(tmp_path, now=local("23:59"))
    assert again["already_handled"] and len(calls) == 1 and old == path.read_bytes()


def test_completed_historical_receipt_is_preserved(tmp_path, monkeypatch):
    path = tmp_path / "data/plan-forward/recovery/evening" / (DAY + ".json")
    record = {"date": DAY, "status": "ok", "settled": [[DAY, "settled"]], "review": {"file": "old"}}
    recovery.save(path, record)
    before = path.read_bytes()
    monkeypatch.setattr(evening, "collect", lambda *a: pytest.fail("already completed"))
    assert evening.run_evening(tmp_path, now=local("22:02")) == dict(record, already_handled=True)
    assert path.read_bytes() == before


def test_busy_calendar_error_holiday_and_next_session(tmp_path, monkeypatch):
    root = tmp_path / "data/plan-forward/recovery/evening"
    monkeypatch.setattr(evening, "collect", lambda *a: pytest.fail("should skip"))
    with recovery.single_writer(root):
        assert evening.run_evening(tmp_path, now=local("22:00"))["status"] == "busy"
    assert not (root / (DAY + ".json")).exists()
    monkeypatch.setattr(evening, "calendar_days", lambda *a: ({}, []))
    assert evening.run_evening(tmp_path, now=local("22:00"))["status"] == "error"
    set_clock(monkeypatch, "0")
    result = evening.run_evening(tmp_path, now=local("22:05"))
    assert result["status"] == "ok" and result["state"] == "exchange_closed"
    assert evening.run_evening(tmp_path, now=local("22:00", "2026-11-20"))["date"] == "2026-11-20"


def test_failed_source_can_retry_and_cannot_claim_verified_evidence(tmp_path, monkeypatch):
    set_clock(monkeypatch, "1")
    r = observations(DAY)
    r["supplemental"]["steps"]["margin"]["dates"][DAY] = {"status": "empty"}
    r["supplemental"]["steps"]["short_public"] = {
        "status": "error",
        "reason": "public_digest_mismatch",
    }
    monkeypatch.setattr(evening, "collect", lambda p, n: r)
    bad = evening.run_evening(tmp_path, now=local("22:00"))
    assert bad["status"] == "attention" and bad["short_margin"][DAY] == "empty"
    assert bad["short_public"] == "needs_review:public_digest_mismatch"
    assert "completed_at" not in bad
    monkeypatch.setattr(evening, "collect", lambda p, n: observations(DAY))
    assert evening.run_evening(tmp_path, now=local("22:02"))["status"] == "ok"


@pytest.mark.parametrize("day,at", [(DAY, "22:00"), ("2026-11-20", "07:40")])
def test_timed_earnings_refresh_keeps_retry_and_early_data_distinct(tmp_path, monkeypatch, day, at):
    now = local(at, day)
    records = [local("18:03")]

    def asof(root, cutoff):
        latest = records[-1]
        meta = dict(
            state="verified",
            manifest="earnings/fixture.json",
            sources=[{}],
            available_at=latest.isoformat(),
            observed_at=latest.isoformat(),
        )
        return meta, {"72030": []}

    monkeypatch.setattr(recovery.earnings, "asof", asof)
    monkeypatch.setattr(
        recovery.earnings, "capture", lambda root: records.append(now) or {"state": "captured"}
    )
    assert recovery.schedule_input(tmp_path, DAY, lambda: now)["status"] == "recovered"
    second = recovery.schedule_input(tmp_path, DAY, lambda: now)
    assert second["status"] == "ready" and second["files"] == 1 and len(records) == 2


@pytest.mark.parametrize("existing_book", [False, True])
def test_native_paper_continues_without_advancing_the_retired_books(
    tmp_path, monkeypatch, existing_book
):
    now = local("22:01")
    root = tmp_path / "data/plan-forward/recovery"
    steps = {"native_evening": {"status": "ready", "capital": 3_000_000}}
    source = tmp_path / "data/swing-forward"
    if existing_book:
        for filename in ("initial.json", f"sessions/{PREVIOUS}/close.json"):
            recovery.save(source / filename, {"historical": True})
        steps.update(legacy_close={"status": "error"}, review={"status": "running"})
    before = {p: p.read_bytes() for p in source.rglob("*.json")}
    recovery.save(
        root / "latest.json",
        dict(status="ok", target=DAY, next_session="2026-11-20", steps=steps),
    )
    monkeypatch.setattr(recovery, "public", lambda *a: recovery.result("ready"))
    monkeypatch.setattr(recovery.a0_5, "enabled", lambda p: True)
    monkeypatch.setattr(recovery.a0_5.book, "load", lambda *a: {"plans": {}})
    calls = []
    monkeypatch.setattr(
        recovery.a0_5,
        "run",
        lambda p, day, *, capital, now: calls.append((day, capital)) or {"state": "ready"},
    )
    r = recovery.recover_supplemental(tmp_path, DAY, now=now)
    assert r["status"] == "ok" and calls == [(DAY, 1_000_000)]
    assert r["steps"]["native_positions"]["accounts"][1_000_000]["status"] == "recovered"
    assert "legacy_close" not in r["steps"] and "review" not in r["steps"]
    assert {p: p.read_bytes() for p in source.rglob("*.json")} == before
    assert source.exists() == existing_book


def test_short_public_after_2200_requires_one_full_new_archive(tmp_path, monkeypatch):
    root = tmp_path / "data/free-market/public"
    root.mkdir(parents=True)
    db_path = root / "public.sqlite"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE observations(id INTEGER PRIMARY KEY,run,url,state,raw_path,sha256,"
            "observed_at,available_at)"
        )
    urls = [recovery.shorts.JSF + s + ".csv" for s in ("meigara", "zandaka", "seigenichiran")]
    urls += [
        recovery.shorts.SHORT_PAGE + "index.html",
        recovery.shorts.SHORT_PAGE + "archive/20261119_Stocks_Restricted_Nextday.csv",
    ]

    def seed(run, stamp):
        with sqlite3.connect(db_path) as db:
            for i, url in enumerate(urls):
                p = root / (run + str(i))
                body = url.encode()
                p.write_bytes(body)
                db.execute(
                    "INSERT INTO observations VALUES(NULL, ?, ?, 'complete', ?, ?, ?, ?)",
                    [run, url, p.name, hashlib.sha256(body).hexdigest(), stamp, stamp],
                )

    seed("short-early", local("19:55").isoformat())
    calls = []

    def capture(project, day):
        calls.append(day)
        seed("short-late", local("22:01").isoformat())
        return "acquired"

    monkeypatch.setattr(recovery.shorts, "capture_public", capture)

    def tick():
        return local("22:02")

    assert recovery.public(tmp_path, DAY, tick)["status"] == "recovered"
    assert recovery.public(tmp_path, DAY, tick)["status"] == "ready" and calls == [DAY]
    (root / "short-late1").write_bytes(b"changed")
    with pytest.raises(ValueError, match="public_digest_mismatch"):
        recovery.public(tmp_path, DAY, tick)


def test_incomplete_source_proof_never_creates_completion(tmp_path, monkeypatch):
    set_clock(monkeypatch, "1")
    r = observations(DAY)
    del r["supplemental"]["steps"]["earnings"]
    monkeypatch.setattr(evening, "collect", lambda *a: r)
    checked = evening.run_evening(tmp_path, now=local("22:00"))
    assert checked["status"] == "attention" and "completed_at" not in checked
