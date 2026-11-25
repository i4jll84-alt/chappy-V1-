from datetime import datetime

import pandas as pd
import pytest

from trading_system.research import a0_5 as native
from trading_system.research import a0_5_book as ledger
from trading_system.research import plan_a_forward as a
from trading_system.research import plan_b_forward as b

SIGNAL = "2026-11-19"
TARGET = "2026-11-20"
CAPITAL = 3_000_000


def local(date, hour):
    return datetime.fromisoformat(date + "T" + hour + "+09:00")


@pytest.mark.parametrize("night_data", [True, False])
def test_a_uses_frozen_night_window_or_actual_preopen_sources(tmp_path, monkeypatch, night_data):
    calls = []
    monkeypatch.setattr(a, "schedule", lambda *_: (TARGET, "2026-12-01", []))

    def prepare(project, signal, target, due, cutoff):
        calls.append(cutoff)
        if not night_data and cutoff.date().isoformat() == SIGNAL:
            raise ValueError("jsf not yet observed")
        return dict(
            strategy=a.STRATEGY,
            signal_date=signal,
            target=target,
            due=due,
            picks=[],
            status="ready",
            cutoff=cutoff.isoformat(),
        )

    monkeypatch.setattr(a, "prepare", prepare)
    book = tmp_path / "A"

    def tick(hour):
        return a.advance(tmp_path, book, start=SIGNAL, capital=CAPITAL, now=local(TARGET, hour))

    status = tick("07:55")
    frozen = book / "sessions" / TARGET / "decision.json"
    assert calls[0] == local(SIGNAL, "23:59:59")
    assert len(calls) == (1 if night_data else 2)
    assert status["decision_origin"] == ("evening_inputs" if night_data else "morning_recovery")
    assert (
        local(TARGET, "07:55")
        <= datetime.fromisoformat(status["decision_recorded_at"])
        < local(TARGET, "09:00")
    )
    if not night_data:
        assert datetime.fromisoformat(status["decision_cutoff"]) >= local(TARGET, "07:55")
    before = frozen.read_bytes()
    tick("08:45")
    assert frozen.read_bytes() == before
    assert len(calls) == (1 if night_data else 2)


@pytest.fixture
def native_account(tmp_path, monkeypatch):
    root = native.root_for(tmp_path, CAPITAL)
    account = tmp_path / "data/plan-forward/accounts/B"
    state = ledger.initial(CAPITAL)
    state["started_at"] = local(SIGNAL, "00:01").isoformat()
    ledger.save(root, state)
    (native.root_for(tmp_path) / "enabled").touch()
    days = pd.to_datetime(["2026-11-18", SIGNAL, TARGET, "2026-11-24", "2026-11-25"])
    monkeypatch.setattr(native.inputs, "calendar", lambda *args: days)
    b.advance(tmp_path, account, start=SIGNAL, capital=CAPITAL, now=local(SIGNAL, "07:30"))
    return root, account


def test_b_recovers_before_open_preserves_revisions_and_guard_cutoff(
    tmp_path, monkeypatch, native_account
):
    root, book = native_account
    calls = []

    def assess(project, day, cutoff, state, *, preopen_target):
        calls.append(cutoff)
        assert day == pd.Timestamp(SIGNAL) and preopen_target == TARGET
        if len(calls) == 1:
            raise ValueError("margin receipt missing")
        return dict(
            state="ready",
            date=SIGNAL,
            order_date=TARGET,
            cutoff=cutoff.isoformat(),
            equity=CAPITAL,
            candidates=[],
            orders=[],
            holding_sectors={},
        )

    monkeypatch.setattr(native, "assess", assess)

    def tick(hour):
        return b.recover_morning(
            tmp_path,
            book,
            signal_date=SIGNAL,
            start=SIGNAL,
            capital=CAPITAL,
            now=local(TARGET, hour),
        )

    assert tick("07:30")["state"] == "blocked"
    blocked = list((root / "sessions" / SIGNAL).glob("*/decision.json"))[0]
    before = blocked.read_bytes()
    recovered = tick("08:15")
    assert recovered["state"] == "ready" and recovered["decision_origin"] == "morning_recovery"
    assert (
        local(TARGET, "08:15")
        <= datetime.fromisoformat(recovered["cutoff"])
        < local(TARGET, "09:00")
    )
    rev = ledger.load(root, CAPITAL)["revision"]
    assert tick("08:25")["frozen"]
    assert len(calls) == 2 and ledger.load(root, CAPITAL)["revision"] == rev
    assert before == blocked.read_bytes()
    assert (
        b.advance(tmp_path, book, start=SIGNAL, capital=CAPITAL, now=local(TARGET, "08:26"))[
            "last_mark"
        ]
        == SIGNAL
    )


@pytest.mark.parametrize("crosses", [False, True])
def test_b_never_generates_late_order(tmp_path, monkeypatch, native_account, crosses):
    root, _ = native_account
    elapsed = [100.0]
    monkeypatch.setattr(native, "monotonic", lambda: elapsed[0])

    def assess(*args, **kwargs):
        assert crosses
        elapsed[0] += 2
        return dict(state="ready", date=SIGNAL, order_date=TARGET, orders=[{"side": "BUY"}])

    monkeypatch.setattr(native, "assess", assess)
    hour = "08:59:59" if crosses else "09:00"
    result = native.recover_morning(tmp_path, SIGNAL, now=local(TARGET, hour))
    assert result["state"] == "missed_preopen" and result["orders"] == 0
    state = ledger.load(root, CAPITAL)
    assert state["plans"][SIGNAL]["orders"] == [] and not state["journal"]
    replay = native.recover_morning(tmp_path, SIGNAL, now=local(TARGET, "09:05"))
    assert replay["state"] == "missed_preopen" and replay["frozen"]
    assert ledger.load(root, CAPITAL)["revision"] == state["revision"]


@pytest.mark.parametrize("stop_time,recheck_time", [("08:02", "08:58"), ("09:02", "09:12")])
def test_b_does_not_rewrite_an_original_decision_or_ignore_stop(
    tmp_path, monkeypatch, native_account, stop_time, recheck_time
):
    root, _ = native_account

    def unexpected(*args, **kwargs):
        pytest.fail("must preserve evening revision")

    monkeypatch.setattr(native, "assess", unexpected)
    s = ledger.load(root, CAPITAL)
    previous = dict(
        date=SIGNAL,
        order_date=TARGET,
        state="ready",
        orders=[],
        candidates=[],
        equity=CAPITAL,
        cutoff=local(SIGNAL, "18:30").isoformat(),
    )
    native.store_decision(root, s, previous)
    rv = s["revision"]
    result = native.recover_morning(tmp_path, SIGNAL, now=local(TARGET, "07:55"))
    assert result["frozen"] and result["cutoff"] == previous["cutoff"]
    assert ledger.load(root, CAPITAL)["revision"] == rv
    stop = native.root_for(tmp_path) / "STOP"
    stop.touch()
    try:
        assert (
            native.recover_morning(tmp_path, SIGNAL, now=local(TARGET, stop_time))["state"]
            == "stopped"
        )
    finally:
        stop.unlink()
    notice = a.swing.read(root / "stops" / (TARGET + ".json"))
    assert datetime.fromisoformat(notice["observed_at"]) >= local(TARGET, stop_time)
    assert notice["phase"] == ("preopen" if stop_time < "09:00" else "native_open_reconciliation")
    assert (
        native.recover_morning(tmp_path, SIGNAL, now=local(TARGET, recheck_time))["state"]
        == "stopped"
    )
    assert ledger.load(root, CAPITAL)["plans"][SIGNAL] == previous


def test_b_before_start_and_holiday_wait_do_not_write(tmp_path, native_account):
    root, _ = native_account
    before = ledger.load(root, CAPITAL)["revision"]
    assert (
        native.recover_morning(tmp_path, "2026-11-18", now=local(SIGNAL, "07:55"))["state"]
        == "outside_comparison"
    )
    assert (
        native.recover_morning(tmp_path, TARGET, now=local("2026-11-21", "08:00"))["state"]
        == "waiting_for_target"
    )
    assert ledger.load(root, CAPITAL)["revision"] == before


def test_after_open_reconciles_only_previously_frozen_orders_once(
    tmp_path, monkeypatch, native_account
):
    root, _ = native_account
    original = {
        "date": SIGNAL,
        "order_date": TARGET,
        "state": "ready",
        "candidates": [],
        "equity": CAPITAL,
        "orders": [
            {
                "Code": "12340",
                "shares": 100,
                "side": "BUY",
                "order_date": TARGET,
                "S33": "0050",
                "reason": "A0_5",
            }
        ],
    }
    native.store_decision(root, ledger.load(root, CAPITAL), original)
    monkeypatch.setattr(native, "assess", lambda *_: pytest.fail("retrospective selection"))
    monkeypatch.setattr(
        native,
        "dated_inputs",
        lambda *args: ({"12340": [{"O": 100, "AdjFactor": 1, "ExRT": ""}]}, {}, []),
    )
    first = native.recover_morning(tmp_path, SIGNAL, now=local(TARGET, "16:35"))
    settled = ledger.load(root, CAPITAL)
    assert first["frozen"] and settled["journal"][0]["status"] == "仮想約定"
    assert len(settled["positions"]) == len(settled["journal"]) == 1
    native.recover_morning(tmp_path, SIGNAL, now=local(TARGET, "16:40"))
    assert ledger.load(root, CAPITAL)["revision"] == settled["revision"]


def test_preopen_earnings_uses_prior_verified_snapshot_if_no_newer_one(tmp_path, monkeypatch):
    seen = []

    def asof(root, ts):
        seen.append(ts)
        if ts.date().isoformat() == TARGET:
            return dict(state="unavailable", reason="no_snapshot_by_cutoff"), {}
        return dict(
            state="verified",
            manifest="local-source",
            available_at=local(SIGNAL, "20:10").isoformat(),
        ), {"12340": [["2026-12-01"]]}

    monkeypatch.setattr(native.inputs.swing_earnings, "asof", asof)
    result, ref = native.inputs.schedules(
        tmp_path, ["12340"], pd.Timestamp(SIGNAL), local(TARGET, "07:55"), {}
    )
    assert result["12340"][0] == "2026-12-01" and ref["checked_on"] == SIGNAL
    assert [x.date().isoformat() for x in seen] == [TARGET, SIGNAL]


def test_two_morning_only_starts_settle_prior_fills_and_freeze_next_candidates(
    tmp_path, monkeypatch, native_account
):
    root, b_book = native_account
    a_book = tmp_path / "A"
    next_session = "2026-11-24"  # After a weekend and an exchange holiday.
    sessions = {SIGNAL: (TARGET, "2026-12-01", []), TARGET: (next_session, "2026-12-02", [])}
    monkeypatch.setattr(a, "schedule", lambda project, signal, now, term: sessions[signal])
    source_times = []

    def a_screen(project, signal, target, due, cutoff):
        if cutoff.date().isoformat() != target:
            raise ValueError("price and credit receipts arrive only the next morning")
        source_times.append(cutoff)
        code = "10000" if signal == SIGNAL else "20000"
        return dict(
            strategy=a.STRATEGY,
            signal_date=signal,
            target=target,
            due=due,
            picks=[dict(rank=1, code=code, prev_close="100", atr="5")],
            status="ready",
            cutoff=cutoff.isoformat(),
        )

    def b_screen(project, day, cutoff, state, *, preopen_target):
        assert cutoff.date().isoformat() == preopen_target
        if day == pd.Timestamp(TARGET):
            assert state["positions"][0]["Code"] == "10000"
            assert state["last_open"] == TARGET
        code = "10000" if day == pd.Timestamp(SIGNAL) else "20000"
        return dict(
            state="ready",
            date=native.iso(day),
            order_date=preopen_target,
            cutoff=cutoff.isoformat(),
            equity=state["cash"] + 95 * sum(p["shares"] for p in state["positions"]),
            candidates=[dict(Code=code)],
            orders=[
                dict(
                    side="BUY",
                    Code=code,
                    shares=100,
                    order_date=preopen_target,
                    S33="0050",
                    reason="A0_5",
                )
            ],
        )

    def late_daily_prices(project, days, asof):
        assert days == [TARGET] and asof.date().isoformat() == next_session
        return (
            {
                "10000": [
                    dict(
                        Date=TARGET, Code="10000", O=100, L=85, H=105, C=95, AdjFactor=1, ExRT=None
                    )
                ]
            },
            {},
            [dict(available_at=local(next_session, "07:37").isoformat())],
        )

    monkeypatch.setattr(a, "prepare", a_screen)
    monkeypatch.setattr(native, "assess", b_screen)
    monkeypatch.setattr(a, "dated_inputs", late_daily_prices)
    monkeypatch.setattr(native, "dated_inputs", late_daily_prices)

    def morning(signal, today):
        now = local(today, "07:40")
        rec = b.recover_morning(
            tmp_path, b_book, signal_date=signal, start=SIGNAL, capital=CAPITAL, now=now
        )
        assert rec["state"] == "ready"
        a_status = a.advance(tmp_path, a_book, start=SIGNAL, capital=CAPITAL, now=now)
        b_status = b.advance(tmp_path, b_book, start=SIGNAL, capital=CAPITAL, now=now)
        assert a_status["next_entry"] == b_status["next_entry"] == today
        return a_status, b_status

    first, first_b = morning(SIGNAL, TARGET)
    assert first["cash"] == str(CAPITAL) and first_b["fills"] == 0
    second, second_b = morning(TARGET, next_session)
    close = a.swing.read(a_book / "sessions" / TARGET / "close.json")
    assert [e["action"] for e in close["events"]] == ["BUY", "SELL_STOP_INTRADAY"]
    assert second["last_mark"] == second_b["last_mark"] == TARGET
    assert second["decision_origin"] == second_b["decision_origin"] == "morning_recovery"
    assert not second["positions"] and float(second["nav"]) == pytest.approx(CAPITAL - 1029.5)
    assert second_b["positions"] == 1 and second_b["fills"] == 1
    state = ledger.load(root, CAPITAL)
    assert state["journal"][0]["date"] == TARGET
    assert [s.date().isoformat() for s in source_times] == [TARGET, next_session]
    morning(TARGET, next_session)
    assert ledger.load(root, CAPITAL)["revision"] == state["revision"]
