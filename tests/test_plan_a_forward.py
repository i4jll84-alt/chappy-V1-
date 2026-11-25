from datetime import datetime
from decimal import Decimal

import pytest

from trading_system.research import plan_a_forward as plan


def local(stamp):
    return datetime.fromisoformat(stamp + "+09:00")


def choice(code, rank=1, close=100, atr=5):
    return {"code": code, "rank": rank, "prev_close": str(close), "atr": str(atr)}


def decision(picks, signal="2026-11-19", target="2026-11-20", due="2026-12-01"):
    return dict(
        strategy=plan.STRATEGY,
        signal_date=signal,
        target=target,
        due=due,
        picks=picks,
        status="ready",
    )


def bar(day, code, op=100, low=99, close=100):
    return dict(Date=day, Code=code, O=op, H=max(op, close), L=low, C=close, AdjFactor=1, ExRT=None)


def test_retry_freeze_and_late_quote_are_idempotent(tmp_path, monkeypatch):
    book = tmp_path / "account"
    start = "2026-11-19"
    monkeypatch.setattr(
        plan,
        "schedule",
        lambda project, d, now, term: {
            start: ("2026-11-20", "2026-12-01", []),
            "2026-11-20": ("2026-11-24", "2026-12-02", []),
        }[d],
    )

    def tick(stamp):
        return plan.advance(tmp_path, book, start=start, capital=3_000_000, now=local(stamp))

    assert tick("2026-11-19T00:10")["status"] == "waiting_signal"

    def missing(*args):
        raise FileNotFoundError("not available")

    monkeypatch.setattr(plan, "prepare", missing)
    assert tick("2026-11-19T18:00")["status"] == "waiting_inputs"
    assert not (book / "sessions").exists()

    def ready(project, signal, target, due, cutoff):
        assert cutoff == local("2026-11-19T23:59:59")
        return decision([choice("10000")], signal, target, due)

    monkeypatch.setattr(plan, "prepare", ready)
    result = tick("2026-11-20T07:55")
    assert result["decision_status"] == "ready" and result["cash"] == "3000000"
    folder = book / "sessions/2026-11-20"
    frozen = (folder / "decision.json").read_bytes()
    monkeypatch.setattr(plan, "prepare", missing)
    assert tick("2026-11-20T09:01")["picks"][0]["code"] == "10000"
    monkeypatch.setattr(plan, "dated_inputs", missing)
    assert tick("2026-11-20T16:00")["status"] == "waiting_close_inputs"
    assert not (folder / "close.json").exists()
    monkeypatch.setattr(
        plan,
        "dated_inputs",
        lambda root, days, now: (
            {"10000": [bar("2026-11-20", "10000", 100, 98, 101)]},
            {},
            [{"verified": True}],
        ),
    )
    result = tick("2026-11-20T18:00")
    assert result["last_mark"] == "2026-11-20"
    assert Decimal(result["nav"]) == Decimal("3000085")
    assert Decimal(result["cash"]) == Decimal("2989985")
    assert result["status"] == "waiting_inputs"
    assert (folder / "decision.json").read_bytes() == frozen
    snapshot = (folder / "close.json").read_bytes()
    assert tick("2026-11-20T20:10") == result
    assert (folder / "close.json").read_bytes() == snapshot
    with pytest.raises(ValueError, match="configuration_changed"):
        plan.advance(tmp_path, book, start=start, capital=2_000_000, now=local("2026-11-20T20:10"))


def test_no_retrospective_orders_and_stop_is_respected(tmp_path, monkeypatch):
    monkeypatch.setattr(plan, "schedule", lambda *args: ("2026-11-20", "2026-12-01", []))

    def unexpected(*args):
        pytest.fail("should not screen names after deadline or while stopped")

    monkeypatch.setattr(plan, "prepare", unexpected)
    book = tmp_path / "account"
    stopped = tmp_path / "data/swing-forward/STOP"
    stopped.parent.mkdir(parents=True)
    stopped.touch()

    def tick(book, ts):
        return plan.advance(tmp_path, book, start="2026-11-19", capital=3_000_000, now=local(ts))

    early = tick(book, "2026-11-19T20:00")
    assert early["decision_status"] == "stopped" and early["picks"] == []
    stopped.unlink()
    assert tick(book, "2026-11-20T08:00")["decision_status"] == "stopped"
    late = tick(tmp_path / "missed", "2026-11-20T09:00")
    assert late["decision_status"] == "missed_preopen" and late["picks"] == []


def test_uncapped_open_gap_cash_and_stop(monkeypatch, tmp_path):
    day = "2026-11-20"
    prior = plan.initial("2026-11-19", 3_000_000)

    def holding(when):
        return dict(
            entered="2026-11-10",
            due=when,
            entry="100",
            atr="2",
            stop="96",
            shares="100",
            debit="10015",
            rank=1,
            quote="100",
        )

    prior["positions"] = {str(i): holding("2026-12-01") for i in range(1, 6)}
    prior["positions"]["6"] = holding(day)
    prior["cash"] = str(3_000_000 - 6 * 10015)
    rows = {c: [bar(day, c)] for c in prior["positions"]}
    rows["new"] = [bar(day, "new", 103, 80, 99)]  # +3.00% is allowed, same-day 2ATR stop at 93.
    rows["higher"] = [bar(day, "higher", 103.01, 101, 102)]  # +3.01% skipped.
    monkeypatch.setattr(plan, "dated_inputs", lambda *args: (rows, {}, []))
    pre = decision([choice("new"), choice("higher", 2), choice("6", 3)])
    data = plan.settle(tmp_path, prior, pre, local("2026-11-20T17:00"))
    assert data["mark"]["peak_positions"] == 6
    assert [(e["code"], e["action"]) for e in data["events"]] == [
        ("6", "SELL_TIME"),
        ("new", "BUY"),
        ("higher", "SKIP"),
        ("6", "SKIP"),
        ("new", "SELL_STOP_INTRADAY"),
    ]
    assert [e["reason"] for e in data["events"] if e["action"] == "SKIP"] == ["gap", "sold_today"]
    assert Decimal(data["events"][-1]["price"]) == 93

    prior = plan.initial("2026-11-19", 20_000)
    prior.update(positions={"6": holding(day)}, cash="9985")
    data = plan.settle(tmp_path, prior, decision([choice("new")]), local("2026-11-20T17:00"))
    assert data["events"][-1]["reason"] == "cash"
    assert data["account"]["positions"] == {}


@pytest.mark.parametrize("switch", ["book", "legacy"])
@pytest.mark.parametrize("first_seen,will_cancel", [("08:59", True), ("09:00", False)])
def test_frozen_candidates_respect_only_a_witnessed_preopen_stop(
    tmp_path, monkeypatch, switch, first_seen, will_cancel
):
    book = tmp_path / "account"
    start, target = "2026-11-19", "2026-11-20"

    def calendar(project, day, now, term):
        return {start: (target, "2026-12-01", []), target: ("2026-11-24", "2026-12-02", [])}[day]

    monkeypatch.setattr(plan, "schedule", calendar)

    def pick(project, signal, target, due, cutoff):
        if signal != start:
            raise FileNotFoundError("next session input pending")
        return decision([choice("new")], signal, target, due)

    monkeypatch.setattr(plan, "prepare", pick)
    rows = {code: [bar(target, code)] for code in ("new", "held")}
    monkeypatch.setattr(plan, "dated_inputs", lambda *args: (rows, {}, []))

    def tick(day, time):
        return plan.advance(
            tmp_path, book, start=start, capital=3_000_000, now=local(f"{day}T{time}")
        )

    assert tick(start, "20:10")["picks"][0]["code"] == "new"
    folder = book / "sessions" / target
    raw = (folder / "decision.json").read_bytes()
    flag = book / "STOP" if switch == "book" else tmp_path / "data/swing-forward/STOP"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    result = tick(target, first_seen)
    assert result["decision_status"] == ("stopped_preopen" if will_cancel else "ready")
    assert result["picks"] == ([] if will_cancel else [choice("new")])
    flag.unlink()
    assert tick(target, "09:15")["decision_status"] == result["decision_status"]
    stopped = folder / "preopen_stop.json"
    assert stopped.exists() == will_cancel
    receipt = plan.swing.read(stopped) if will_cancel else None
    stop_bytes = stopped.read_bytes() if will_cancel else None
    tick(target, "17:00")
    close = plan.swing.read(folder / "close.json")
    original = plan.swing.read(folder / "decision.json")
    assert (folder / "decision.json").read_bytes() == raw
    assert close["decision_sha256"] == plan.swing.signature(original)
    assert close["preopen_stop_sha256"] == (plan.swing.signature(receipt) if receipt else None)
    assert [e["action"] for e in close["events"]] == ([] if will_cancel else ["BUY"])
    if will_cancel:
        assert receipt["canceled_buy_candidates"] == ["new"] and stopped.read_bytes() == stop_bytes
        # A stopped buy basket still processes the original holding's scheduled exit.
        previous = plan.initial(start, 3_000_000)
        previous["positions"]["held"] = dict(
            entered="2026-11-10",
            due=target,
            entry="100",
            atr="2",
            stop="96",
            shares="100",
            debit="10015",
            rank=1,
            quote="100",
        )
        previous["cash"] = "2989985"
        closed = plan.settle(tmp_path, previous, original, local(target + "T17:00"), receipt)
        assert [(e["code"], e["action"]) for e in closed["events"]] == [("held", "SELL_TIME")]
