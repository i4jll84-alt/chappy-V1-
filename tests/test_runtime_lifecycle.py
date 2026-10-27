"""Acceptance tests use fixed calendars, mock accounts and virtual clocks only."""

import json
import socket
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta

import pytest

from trading_system.brokers.demo import fixture, request
from trading_system.brokers.registry import Config
from trading_system.brokers.router import BrokerRouter
from trading_system.execution.demo import NOW
from trading_system.runtime.handlers import Fixtures, Response
from trading_system.runtime.models import JST, Calendar, Day, Settings
from trading_system.runtime.service import Lifecycle, run
from trading_system.runtime.store import JobStore


def at(day="2026-01-05", time="10:00"):
    return datetime.fromisoformat(day + "T" + time).replace(tzinfo=JST)


def settings(**changes):
    # Explicit fixture, includes New Year closure and holiday Monday Jan 12.
    days = [
        Day(day=date(2026, 1, 1) + timedelta(days=i), business=i in {3, 4, 5, 6, 7, 8, 12, 13, 14})
        for i in range(15)
    ]
    # Jan 4 is explicitly synthetic business day to give Jan 5 prerequisite history.
    values = dict(
        first_day=date(2026, 1, 4), calendar=Calendar(known_at=at("2026-01-01"), days=tuple(days))
    )
    values.update(changes)
    return Settings(**values)


def responses(days=("2026-01-04", "2026-01-05"), available="16:30"):
    result = {}
    for day in days:
        price = dict(
            Date=day,
            Code="31050",
            O=100,
            H=101,
            L=99,
            C=100,
            Vo=1000,
            Va=100000,
            AdjFactor=1,
            AdjO=100,
            AdjH=101,
            AdjL=99,
            AdjC=100,
            AdjVo=1000,
        )
        master = dict(
            Date=day, Code="31050", CoName="SIMULATED", Mkt="0111", S17="1", S33="1", ProdCat="011"
        )
        for kind, rows, keys in (
            ("prices", [price], ["31050"]),
            ("topix", [dict(Date=day, O=100, H=101, L=99, C=100)], [day]),
            ("master", [master], ["31050"]),
            ("financials", [], []),
        ):
            result[kind + ":" + day] = (
                Response(
                    available_at=at(day, available),
                    target_day=day,
                    expected_keys=tuple(keys),
                    pages=(tuple(rows),),
                    empty_reason="no_disclosure" if kind == "financials" else None,
                ),
            )
    return Fixtures(responses=result)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network_forbidden_in_stage_A")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def harness(tmp_path):
    opened = []

    def make(now=None, config=None, data=None, root=None):
        root = root or tmp_path
        clock = [now or at()]
        router = BrokerRouter(root / "paper", Config(), fixture())
        life = Lifecycle(
            root / "lifecycle", config or settings(), router, data or responses(), lambda: clock[0]
        )
        opened.append((life, router))
        return life, router, clock

    yield make
    for life, router in reversed(opened):
        if getattr(life, "_test_closed", False):
            continue
        life.close()
        router.close()


def close(life, router):
    life.close()
    router.close()
    life._test_closed = True


def test_1600_normal_stop_preserves_orders_without_cancel_or_sale(harness):
    life, router, clock = harness()
    for _ in range(15):
        life.tick()
    order = router.submit(request(router.active, "kept"), NOW)
    router.adapter.simulate_fill("kept", 40, "100", "partial", NOW)
    clock[0] = at(time="15:30")
    assert life.tick()["purpose"] == "preparing_stop"
    clock[0] = at(time="16:00")
    status = life.tick()
    assert status["normal_exit"] and status["stop_reason"] == "daytime_1600"
    assert status["real_execution_enabled"] is False
    persisted = router.adapter.manager.store.order("kept")
    assert persisted["state"] == "PARTIALLY_FILLED"
    assert persisted["send_attempt_count"] == 1
    assert order.broker_order_id == persisted["broker_id"]
    assert life.store.meta("last_normal_exit") == clock[0].isoformat()
    assert any(j["day"] == "2026-01-05" for j in life.store.jobs())
    with pytest.raises(ValueError, match="blocked"):
        router.cancel("kept", NOW)


@pytest.mark.parametrize("time", ["16:10", "19:00", "22:00"])
def test_postprocessing_restart_runs_not_killed_by_1600(harness, time):
    life, router, clock = harness(at(time=time))
    assert life.purpose == "postprocessing"
    status = life.tick()
    assert status["normal_exit"] and status["jobs"].get("completed", 0) > 0
    assert status["purpose"] == "postprocessing"
    if time == "16:10":
        assert status["jobs"]["publication_wait"] == 4
        assert any(j["state"] == "dependency_wait" for j in life.store.jobs())
    else:
        assert status["jobs"] == {"completed": 15}
        assert len(list(life.store.db.execute("SELECT * FROM outputs"))) == 15


def test_reconcile_timeout_saves_unknown_and_does_not_resend(harness, monkeypatch):
    life, router, clock = harness()
    for _ in range(15):
        life.tick()
    router.submit(request(router.active, "unknown-cancel"), NOW)
    router.adapter.gateway.cancel("unknown-cancel", lose_ack=False)

    def timeout(now):
        raise TimeoutError("SECRET_TOKEN_must_not_be_logged")

    monkeypatch.setattr(router, "reconcile", timeout)
    clock[0] = at(time="16:00")
    status = life.tick()
    assert status["normal_exit"] and not status["reconciliation_complete"]
    assert "unknown-cancel" in status["account"]["unresolved_orders"]
    assert "SECRET_TOKEN" not in json.dumps(status)
    assert router.adapter.manager.store.order("unknown-cancel")["state"] == "ACKNOWLEDGED"


def test_local_save_failure_is_not_normal_or_auto_restarted(harness, monkeypatch):
    life, router, clock = harness()
    previous = life.store.meta("last_normal_exit")

    def fail(*args, **kwargs):
        raise OSError("disk full secret")

    monkeypatch.setattr(life.store, "record_status", fail)
    clock[0] = at(time="16:00")
    result = life.tick()
    assert result["normal_exit"] is False and result["save_error"] == "lifecycle_OSError"
    assert life.store.meta("last_normal_exit") == previous
    with pytest.raises(ValueError, match="blocked"):
        router.submit(request(router.active, "forbidden"), NOW)
    assert life.tick()["save_error"] == result["save_error"]


def test_restart_publication_wait_retry_interval_and_atomic_nonadditive_report(harness):
    life, router, clock = harness(at(time="16:10"))
    life.tick()
    attempts = len(list(life.store.db.execute("SELECT * FROM attempts")))
    old = {r[0]: r[1:] for r in life.store.db.execute("SELECT * FROM outputs")}
    close(life, router)
    second, r2, _ = harness(at(time="16:15"))
    second.tick()
    assert len(list(second.store.db.execute("SELECT * FROM attempts"))) == attempts
    close(second, r2)
    third, _, _ = harness(at(time="19:00"))
    assert third.tick()["jobs"] == {"completed": 15}
    for key, value in old.items():
        assert (
            tuple(
                third.store.db.execute(
                    "SELECT payload,hash FROM outputs WHERE job=?", (key,)
                ).fetchone()
            )
            == value
        )


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("incomplete", "publication_wait"),
        ("duplicate", "needs_review"),
        ("wrong_date", "needs_review"),
        ("empty", "publication_wait"),
        ("invalid_number", "needs_review"),
        ("missing_field", "needs_review"),
    ],
)
def test_missing_partial_bad_data_never_unblocks_candidates(harness, mutation, expected):
    data = responses()
    key = "prices:2026-01-05"
    r = data.responses[key][0].model_dump(mode="json")
    if mutation == "incomplete":
        r["complete"] = False
    elif mutation == "duplicate":
        r["pages"][0].append(dict(r["pages"][0][0]))
    elif mutation == "wrong_date":
        r["pages"][0][0]["Date"] = "2026-01-04"
    elif mutation == "empty":
        r["pages"] = [[]]
    elif mutation == "invalid_number":
        r["pages"][0][0]["AdjFactor"] = -1
    else:
        del r["pages"][0][0]["C"]
    data = data.model_dump(mode="json")
    data["responses"][key] = [r]
    life, _, _ = harness(at(time="19:00"), data=Fixtures.model_validate(data))
    status = life.tick()
    assert status["jobs"].get(expected, 0)
    assert life.store.get(life.candidate_key)["state"] != "completed"


def test_holiday_not_missing_and_no_session(harness):
    life, router, _ = harness(at("2026-01-10", "10:00"))
    assert life.purpose == "postprocessing"
    status = life.tick()
    assert status["normal_exit"]
    assert status["target_day"] == "2026-01-13"
    assert not any(
        j["day"] in {"2026-01-10", "2026-01-11", "2026-01-12"} and j["kind"] in {"prices", "topix"}
        for j in life.store.jobs()
    )
    with pytest.raises(ValueError, match="blocked"):
        router.submit(request(router.active, "holiday"), NOW)


@pytest.mark.parametrize("time", ["08:30", "10:00"])
def test_current_management_priority_missing_new_risk_gate(harness, time):
    life, router, _ = harness(at("2026-01-06", time), data=Fixtures())
    assert life.purpose == "daytime"
    events = []
    original = life.reconcile

    def reconcile():
        events.append("reconcile")
        original()

    life.reconcile = reconcile
    original_step = life.step_job

    def step(**kwargs):
        events.append("data")
        return original_step(**kwargs)

    life.step_job = step
    life.tick()
    assert events == ["reconcile", "data"]
    with pytest.raises(ValueError, match="current_data"):
        router.submit(request(router.active, "no-input"), NOW)
    # Closing risk is not blocked by a missing data/short-inventory check.
    life.guard("submit", request(router.active, "exit", "cash_sell"))
    assert not any(j["kind"] == "report" and j["attempts"] for j in life.store.jobs())


def test_multiday_reconstruction_without_shutdown_and_no_historical_candidates(harness):
    life, router, clock = harness(at(time="10:00"))
    assert not any(j["day"] == "2026-01-05" and j["kind"] == "prices" for j in life.store.jobs())
    close(life, router)  # Crash-like exit before EOD registration.
    next_life, _, _ = harness(at("2026-01-13", "10:00"))
    days = {j["day"] for j in next_life.store.jobs() if j["kind"] == "prices"}
    assert days == {
        "2026-01-04",
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
    }
    next_life.tick()
    assert next_life.store.get(next_life.candidate_key)["day"] == "2026-01-13"
    assert not next_life.router.adapter.manager.store.orders()
    assert not any("train" in j["kind"] for j in next_life.store.jobs())


@pytest.mark.parametrize("operation", ["submit", "cancel", "modify", "switch"])
def test_all_postprocess_order_operations_blocked_in_router_and_adapter(harness, operation):
    life, router, _ = harness(at(time="19:00"))
    calls = {
        "submit": lambda a: a.submit(request(router.active, "bad"), NOW),
        "cancel": lambda a: a.cancel("bad", NOW),
        "modify": lambda a: a.modify("bad", 100, 100, NOW),
        "switch": lambda a: a.switch("tachibana", NOW),
    }
    for target in [router] if operation == "switch" else [router, router.adapter]:
        with pytest.raises(ValueError):
            calls[operation](target)
    assert router.adapter.gateway.order_inventory() == []


def test_guard_uses_clock_even_before_next_tick(harness):
    life, router, clock = harness()
    for _ in range(15):
        life.tick()
    assert life.data_ready()
    clock[0] = at(time="16:00")
    with pytest.raises(ValueError, match="blocked"):
        router.adapter.submit(request(router.active, "late"), NOW)


def test_calendar_gaps_naive_dates_permissions_fail_closed():
    with pytest.raises(ValueError, match="missing_dates"):
        Calendar(
            known_at=NOW,
            days=(
                Day(day=date(2026, 1, 5), business=True),
                Day(day=date(2026, 1, 7), business=True),
            ),
        )
    with pytest.raises(ValueError):
        Settings.model_validate({**settings().model_dump(), "mode": "live"})
    with pytest.raises(ValueError):
        settings().now(datetime(2026, 1, 5))
    with pytest.raises(ValueError, match="coverage"):
        settings().now(at("2026-02-01"))


def test_timezone_equivalence(harness):
    from datetime import timezone

    life, _, clock = harness(at(time="15:59").astimezone(timezone.utc))
    clock[0] = at(time="16:00").astimezone(timezone.utc)
    assert life.tick()["normal_exit"]


def test_definition_correction_preserves_old_output_and_observation_time(harness):
    life, router, _ = harness(at(time="19:00"))
    life.tick()
    originals = dict(life.store.db.execute("SELECT job,hash FROM outputs"))
    close(life, router)
    corrected = responses().model_dump(mode="json")
    corrected["responses"]["prices:2026-01-05"][0]["pages"][0][0]["C"] = 101
    corrected["responses"]["prices:2026-01-05"][0]["pages"][0][0]["AdjC"] = 101
    revised, _, _ = harness(
        at("2026-01-06", "19:00"),
        config=settings(definition_version="v2"),
        data=Fixtures.model_validate(corrected),
    )
    revised.tick()
    for key, value in originals.items():
        assert revised.store.output(key)["hash"] == value
    new = [
        j
        for j in revised.store.jobs()
        if j["version"] == "v2" and j["kind"] == "prices" and j["day"] == "2026-01-05"
    ][0]
    output = revised.store.output(new["key"])["payload"]
    assert output["obtained_at"].startswith("2026-01-06")
    assert output["published_at"] is None


def test_queue_recovery_keeps_checkpoint_and_attempt_history(tmp_path):
    store = JobStore(tmp_path, NOW)
    key = store.ensure("prices", NOW.date(), {}, "v1", [], {})
    job = store.claim(store.get(key), NOW)
    store.checkpoint(job, {"page": 1, "rows": ["saved"]})
    store.close()
    store = JobStore(tmp_path, NOW + timedelta(hours=1))
    try:
        job = store.get(key)
        assert job["state"] == "retry_wait" and job["checkpoint"]["page"] == 1
        attempt = json.loads(store.db.execute("SELECT value FROM attempts").fetchone()[0])
        assert attempt["result"] == "interrupted" and attempt["ended_at"]
    finally:
        store.close()


@pytest.mark.parametrize("commit", [False, True])
def test_process_death_before_after_atomic_output_commit(tmp_path, commit):
    script = """
import os, sys
from pathlib import Path
from trading_system.runtime.store import JobStore
from trading_system.execution.demo import NOW
s=JobStore(Path(sys.argv[1]),NOW)
k=s.ensure('report',NOW.date(),{},'v1',[],{})
j=s.claim(s.get(k),NOW)
if sys.argv[2]=='True':
 s.finish(j,NOW,'completed',output={'count':1,'pnl':'123'})
else:
 s.db.execute('BEGIN IMMEDIATE')
 s.db.execute("INSERT INTO outputs VALUES(?,?,?)",(k,'unfinished','bad'))
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(commit)], capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    store = JobStore(tmp_path, NOW)
    try:
        job = store.jobs()[0]
        assert job["state"] == ("completed" if commit else "retry_wait")
        if commit:
            assert store.output(job["key"])["payload"]["pnl"] == "123"
        else:
            assert store.db.execute("SELECT count(*) FROM outputs").fetchone()[0] == 0
    finally:
        store.close()


def test_two_processes_cannot_claim_same_job(tmp_path):
    store = JobStore(tmp_path, NOW)
    try:
        script = """
import sys
from pathlib import Path
from trading_system.runtime.store import JobStore
from trading_system.execution.demo import NOW
try:
 JobStore(Path(sys.argv[1]), NOW)
except ValueError:
 sys.exit(7)
sys.exit(1)
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)], capture_output=True, timeout=30
        )
        assert result.returncode == 7, result.stderr
    finally:
        store.close()


def test_output_corruption_never_used_as_ready(harness):
    life, router, _ = harness(at(time="19:00"))
    life.tick()
    life.store.db.execute("UPDATE outputs SET hash='bad'")
    close(life, router)
    with pytest.raises(ValueError, match="hash"):
        harness(at("2026-01-06", "08:30"))


def test_run_loop_finishes_at_virtual_1600(harness):
    life, _, clock = harness(at(time="15:59:59"))

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    assert run(life, sleep)["normal_exit"]


def test_retry_limit_not_infinite(harness):
    life, _, _ = harness(at(time="19:00"), config=settings(max_attempts=1), data=Fixtures())
    life.tick()
    pending = next(j for j in life.store.jobs() if j["kind"] == "prices")
    assert pending["attempts"] == 1


def test_paged_acquisition_resume_after_process_style_interruption(harness, monkeypatch):
    data = responses().model_dump(mode="json")
    r = data["responses"]["prices:2026-01-05"][0]
    second = dict(r["pages"][0][0])
    second["Code"] = "72030"
    r["pages"].append([second])
    r["expected_keys"].append("72030")
    data = Fixtures.model_validate(data)
    life, router, _ = harness(at(time="19:00"), data=data)
    original = life.store.checkpoint

    def crash(job, value):
        original(job, value)
        if job["kind"] == "prices" and job["day"] == "2026-01-05":
            raise KeyboardInterrupt()

    monkeypatch.setattr(life.store, "checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        life.tick()
    interrupted = next(
        j for j in life.store.jobs() if j["kind"] == "prices" and j["day"] == "2026-01-05"
    )
    assert interrupted["checkpoint"]["page"] == 1
    close(life, router)
    resumed, _, _ = harness(at(time="22:00"), data=data)
    assert resumed.tick()["normal_exit"]
    output = resumed.store.output(interrupted["key"])["payload"]
    assert output["count"] == 2
    assert len(output["page_observations"]) == 2
    assert output["page_observations"][0]["obtained_at"].startswith("2026-01-05T19:")
    assert output["page_observations"][1]["obtained_at"].startswith("2026-01-05T22:")


@pytest.mark.parametrize("kind", ["features", "report"])
def test_calculation_crash_recomputes_once_without_double_addition(harness, monkeypatch, kind):
    life, router, _ = harness(at(time="19:00"))
    original = life.handlers.execute

    def crash(job, *args):
        result = original(job, *args)
        if job["kind"] == kind:
            raise KeyboardInterrupt()
        return result

    monkeypatch.setattr(life.handlers, "execute", crash)
    with pytest.raises(KeyboardInterrupt):
        life.tick()
    interrupted = next(j for j in life.store.jobs() if j["state"] == "running")
    assert interrupted["kind"] == kind
    close(life, router)
    resumed, _, _ = harness(at(time="22:00"))
    resumed.tick()
    assert resumed.store.get(interrupted["key"])["state"] == "completed"
    assert (
        resumed.store.db.execute(
            "SELECT count(*) FROM outputs WHERE job=?", (interrupted["key"],)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("state", ["UNKNOWN_SUBMIT", "UNKNOWN_CANCEL", "PARTIALLY_FILLED"])
def test_uncertain_or_partial_orders_reservations_survive_stop_restart(harness, monkeypatch, state):
    life, router, clock = harness()
    for _ in range(15):
        life.tick()
    router.submit(request(router.active, "preserve"), NOW)
    if state == "PARTIALLY_FILLED":
        router.adapter.simulate_fill("preserve", 40, "100", "preserve-fill", NOW)
    else:
        order = router.adapter.manager.store.order("preserve")
        order["state"] = state
        with router.adapter.manager.store.transaction():
            router.adapter.manager.store.save_order(order)

    def timeout(now):
        raise TimeoutError()

    monkeypatch.setattr(router, "reconcile", timeout)
    clock[0] = at(time="16:00")
    status = life.tick()
    reserved = status["account"]["reservation_basis"]
    assert len(reserved) == 1
    assert reserved[0]["remaining_quantity"] == (60 if state == "PARTIALLY_FILLED" else 100)
    assert reserved[0]["state"] == state
    assert router.adapter.gateway.snapshot("preserve")["send_attempt_count"] == 1
    close(life, router)
    resumed, r2, _ = harness(at(time="19:00"))
    resumed.tick()
    # Fresh authoritative query may resolve uncertainty; it is not a resubmission.
    assert r2.adapter.gateway.snapshot("preserve")["send_attempt_count"] == 1
    assert r2.adapter.manager.store.order("preserve")["state"] != "CANCELLED"


def test_shutdown_at_deadline_during_reconcile_starts_no_job(harness, monkeypatch):
    life, _, clock = harness(at(time="15:59:59"))
    original = life.reconcile

    def reconcile():
        original()
        clock[0] = at(time="16:00")

    monkeypatch.setattr(life, "reconcile", reconcile)

    def forbidden(**kwargs):
        raise AssertionError("job_started_after_deadline")

    monkeypatch.setattr(life, "step_job", forbidden)
    assert life.tick()["normal_exit"]


def test_save_failure_after_preparation_keeps_last_exit_and_reason(harness, monkeypatch):
    life, _, clock = harness()
    original = life.store.record_status

    def fail(value, normal=False):
        if normal:
            raise sqlite3.OperationalError("disk full")
        return original(value, normal=normal)

    monkeypatch.setattr(life.store, "record_status", fail)
    clock[0] = at(time="16:00")
    status = life.tick()
    assert not status["normal_exit"]
    assert status["delay_reason"] == "lifecycle_OperationalError"
    assert life.store.meta("status")["normal_exit"] is False
    assert life.store.meta("last_normal_exit") is None


def test_lease_owner_cannot_commit_another_attempt(tmp_path):
    store = JobStore(tmp_path, NOW)
    try:
        key = store.ensure("prices", NOW.date(), {}, "v1", [], {})
        job = store.claim(store.get(key), NOW)
        job2 = dict(job)
        job2["owner"] = "another_owner"
        store.save(job2)
        with pytest.raises(ValueError, match="not_owned"):
            store.checkpoint(job, {"page": 100})
    finally:
        store.close()


def test_retry_limit_is_persisted_across_startups(harness):
    cfg = settings(max_attempts=1, retry_seconds=60)
    life, router, _ = harness(at(time="19:00"), config=cfg, data=Fixtures())
    life.tick()
    close(life, router)
    later, _, _ = harness(at(time="19:01"), config=cfg, data=Fixtures())
    status = later.tick()
    assert status["normal_exit"] and status["jobs"]["needs_review"] == 8
    assert all(j["attempts"] <= 1 for j in later.store.jobs())


def test_permission_and_fixture_changes_cannot_silently_replace_definition(tmp_path):
    router = BrokerRouter(tmp_path / "paper", Config(), fixture())
    life = Lifecycle(
        tmp_path / "lifecycle", settings(), router, responses(), lambda: at(time="19:00")
    )
    life.tick()
    close(life, router)
    router = BrokerRouter(tmp_path / "paper", Config(), fixture())
    try:
        with pytest.raises(ValueError, match="new_version"):
            Lifecycle(
                tmp_path / "lifecycle", settings(), router, Fixtures(), lambda: at(time="22:00")
            )
    finally:
        router.close()


def test_no_silent_success_on_corrupt_output_during_run(harness):
    life, _, _ = harness()
    for _ in range(15):
        life.tick()
    life.store.db.execute("UPDATE outputs SET hash='corrupt' WHERE job=?", (life.candidate_key,))
    status = life.tick()
    assert not status["normal_exit"] and status["purpose"] == "needs_review"
    assert not status["current_data_ready"]


def test_bounded_postprocessing_leaves_checkpoint_without_wait_loop(harness):
    life, _, _ = harness(at(time="19:00"), config=settings(postprocess_budget=1))
    result = life.tick()
    assert result["normal_exit"] and result["unfinished"]
    assert sum(j["attempts"] for j in life.store.jobs()) == 1


def test_current_candidate_expiry_is_not_replayed(harness):
    life, router, _ = harness()
    old = life.candidate_key
    close(life, router)
    later, _, _ = harness(at("2026-01-06", "10:00"))
    assert later.store.get(old)["state"] == "needs_review"
    assert later.store.get(old)["reason"] == "expired_candidate_not_replayed"
    assert later.store.get(later.candidate_key)["day"] == "2026-01-06"


def test_virtual_cli_no_connection_and_persists_handoff(tmp_path, capsys):
    from trading_system.runtime.__main__ import main

    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            dict(
                settings=settings().model_dump(mode="json"),
                paper_fixture=fixture().model_dump(mode="json"),
                responses=responses().model_dump(mode="json"),
                ticks=[at(time="15:59").isoformat(), at(time="16:00").isoformat()],
            )
        ),
        encoding="utf-8",
    )
    assert main(["--scenario", str(scenario), "--state-root", str(tmp_path / "state")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["steps"][-1]["normal_exit"]
    assert result["real_execution_enabled"] is False


def test_storage_read_failure_is_explicit_not_normal(harness, monkeypatch):
    life, _, _ = harness()

    def fail(*args):
        raise sqlite3.DatabaseError("private database path")

    monkeypatch.setattr(life.store, "jobs", fail)
    status = life.tick()
    assert not status["normal_exit"]
    assert status["save_error"] == "status_unavailable_DatabaseError"
    assert "private database path" not in json.dumps(status)


def test_order_actions_can_never_be_registered_as_retry_jobs(tmp_path):
    store = JobStore(tmp_path, NOW)
    try:
        for kind in ("submit", "cancel", "modify", "train", "optimize"):
            with pytest.raises(ValueError, match="only_replayable"):
                store.ensure(kind, NOW.date(), {}, "v1", [], {})
        assert store.jobs() == []
    finally:
        store.close()


def test_finished_job_cannot_publish_a_second_output(tmp_path):
    store = JobStore(tmp_path, NOW)
    try:
        key = store.ensure("report", NOW.date(), {}, "v1", [], {})
        job = store.claim(store.get(key), NOW)
        store.finish(job, NOW, "completed", output={"count": 1, "pnl": 1})
        with pytest.raises(ValueError, match="completion_not_owned"):
            store.finish(job, NOW, "completed", output={"count": 1, "pnl": 2})
        assert store.output(key)["payload"]["pnl"] == 1
    finally:
        store.close()
