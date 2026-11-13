from datetime import datetime
from decimal import Decimal as D

import pytest

from trading_system.research import swing_forward as sf


def row(price="100", factor="1"):
    return {"O": price, "C": price, "AdjFactor": factor, "ExRT": None}


def test_independent_cash_and_costed_holding():
    books = sf.initial()
    first = books["agent_current"]
    orders = {"buy": ["12340"], "sell": []}
    state, fills, metrics = sf.settle_account(
        first, orders, {"12340": row()}, "2026-11-16", "2026-11-25"
    )
    assert first["positions"] == {} and books["machine_current"]["cash"] == "2000000"
    assert D(state["cash"]) == D("1989985") and D(metrics["net"]) == -15
    assert fills[0]["quantity"] == 100
    rs = [{"code": "12340", "estimated_debit": "10015"}]
    decision = sf.account_orders(state, rs, "2026-11-25")
    assert decision["buy"] == [] and decision["sell"] == ["12340"]
    done, _, marked = sf.settle_account(
        state, decision, {"12340": row("120")}, "2026-11-25", "unused"
    )
    assert done["positions"] == {}
    assert D(marked["realized"]) == 1969 and D(marked["unrealized"]) == 0
    assert D(marked["fees"]) == 20 and D(marked["slippage"]) == 11


def test_missing_open_and_rights_not_filled():
    initial = sf.initial()["agent_plus3"]
    orders = {"buy": ["12340"], "sell": []}
    result, fills, _ = sf.settle_account(initial, orders, {"12340": row(None)}, "d", "due")
    assert result["positions"] == {} and fills[0]["status"] == "missing_open"
    held, _, _ = sf.settle_account(initial, orders, {"12340": row()}, "d", "due")
    held2, _, nav = sf.settle_account(
        held, {"buy": [], "sell": ["12340"]}, {"12340": row("200", "0.5")}, "due", "x"
    )
    assert (
        held2["status"] == "needs_review"
        and nav is None
        and held2["positions"] == held["positions"]
    )


def test_publication_deadline_repeat_and_settlement(tmp_path, monkeypatch):
    day = "2026-11-16"
    sessions = [
        "2026-11-13",
        day,
        "2026-11-17",
        "2026-11-18",
        "2026-11-19",
        "2026-11-20",
        "2026-11-24",
        "2026-11-25",
    ]
    root = tmp_path / "data/swing-forward"
    sf.write(root / "initial.json", dict(start=day, accounts=sf.initial()))

    class Clock(datetime):
        h = 8

        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 16, cls.h, 0, tzinfo=sf.JST)

    monkeypatch.setattr(sf, "datetime", Clock)
    r = dict(
        code="12340", estimated_debit="10015", plus3_exclusions=["credit_snapshot_unavailable"]
    )
    monkeypatch.setattr(sf, "screen", lambda *args: dict(candidates=[r], credit_complete=False))
    assert sf.morning(tmp_path, root, day, sessions, Clock.now()) == "frozen"
    assert sf.morning(tmp_path, root, day, sessions, Clock.now()) == "already_frozen"
    folder = root / "sessions" / day
    packet = sf.read(folder / "review-input.json")
    assert "machine" not in packet and "candidates" not in packet
    group = dict(buy=[], sell=[], reason="independent hold", candidate_notes={"12340": "hold"})
    answer = dict(
        target=day,
        input_sha256=packet["input_sha256"],
        groups={"current": group, "plus3": dict(group, buy=["12340"])},
    )
    Clock.h = 9
    with pytest.raises(ValueError, match="missed_agent_deadline"):
        sf.record_agent(root, day, answer)
    assert not (folder / "agent.json").exists()
    Clock.h = 8
    sf.record_agent(root, day, answer)
    assert sf.record_agent(root, day, answer) == "already_frozen"
    assert not sf.read(folder / "agent.json")["differences"]["current"]["buy"]["same_order"]
    rejected = sf.read(folder / "agent.json")
    assert rejected["answer"]["groups"]["plus3"]["buy"] == ["12340"]
    assert rejected["executable"]["plus3"]["buy"] == []
    monkeypatch.setattr(sf, "dated_inputs", lambda *args: ({"12340": [row()]}, {}, []))
    Clock.h = 20
    assert sf.closing(tmp_path, root, day, sessions, Clock.now()) == "settled"
    assert sf.closing(tmp_path, root, day, sessions, Clock.now()) == "already_settled"
    close = sf.read(folder / "close.json")
    assert D(close["comparison"]["current"]["net_difference"]) == 15
    assert close["coverage"]["agent_plus3"] == "credit_missing"
    assert len(close["accounts"]) == 4
