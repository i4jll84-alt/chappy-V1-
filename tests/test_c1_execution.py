from copy import deepcopy
from datetime import date, timedelta

import pytest

from trading_system.research import c1_execution as book

# Calendar deliberately jumps weekends. All age/window/T+2 checks use exchange sessions.
CAL = [
    str(date(2026, 10, 1) + timedelta(days=d))
    for d in range(32)
    if (date(2026, 10, 1) + timedelta(days=d)).weekday() < 5
]


def candidate(code, *, sector="one", close=1000, atr=20, deviation=-0.08):
    return dict(code=code, sector=sector, close=close, atr14=atr, deviation=deviation)


def plan(account, i, ranked=(), marks=None, earnings=None, **kw):
    if earnings is None:
        earnings = {r["code"]: [] for r in ranked}
    return book.freeze_plan(account, CAL[i], CAL[i + 1], ranked, marks or {}, earnings, CAL, **kw)


def bars(*codes, opening=1000, low=975, close=990):
    return {c: dict(open=opening, low=low, close=close) for c in codes}


def buy_once(*codes, low=975, close=990, atr=20, stop_atr=2):
    initial = book.initial_account()
    p = plan(initial, 0, [candidate(c, atr=atr) for c in codes], stop_atr=stop_atr)
    return book.execute_day(initial, CAL[1], p, bars(*codes, low=low, close=close), CAL)


def test_rank_gap_exact_boundary_no_promotion_and_frozen_lots():
    a = book.initial_account()
    p = plan(a, 0, [candidate("C"), candidate("B"), candidate("A")])
    assert [r["code"] for r in p["buys"]] == ["A", "B"]
    assert [r["shares"] for r in p["buys"]] == [500, 500]
    prices = {
        "A": dict(open=1020, low=1000, close=1015),
        "B": dict(open=1020.001, low=990, close=1000),
        "C": bars("C")["C"],
    }
    result = book.execute_day(a, CAL[1], p, prices, CAL)
    assert list(result["positions"]) == ["A"]
    assert result["trades"][0]["price"] == pytest.approx(1021.02)
    assert result["trades"][0]["fee"] == pytest.approx(255.255)
    assert [(s["code"], s["reason"]) for s in result["skips"]] == [("B", "opening_gap")]


@pytest.mark.parametrize(
    "blocked,reason",
    [
        (None, "unknown_earnings"),
        ([CAL[1]], "earnings_window"),
        ([CAL[3]], "earnings_window"),
    ],
)
def test_earnings_days_one_through_three_and_unknown_block_without_promotion(blocked, reason):
    a = book.initial_account()
    p = plan(a, 0, [candidate(c) for c in "ABC"], earnings={"A": blocked, "B": [CAL[4]], "C": []})
    assert [r["code"] for r in p["buys"]] == ["B"]
    assert p["skips"][0]["reason"] == reason


@pytest.mark.parametrize(
    "atr,prices,reason",
    [
        (None, {"open": 1000}, "missing_atr"),
        (20, {}, "missing_open"),
        (20, {"open": None}, "missing_open"),
        (20, {"open": 1000, "adj_factor": 0.5}, "corporate_action"),
    ],
)
def test_missing_prices_atr_or_entry_corporate_action_keep_lower_candidate_out(atr, prices, reason):
    a = book.initial_account()
    p = plan(a, 0, [candidate("A", atr=atr), candidate("B"), candidate("C")])
    r = book.execute_open(a, CAL[1], p, {"A": prices, **bars("B", "C")}, CAL)
    assert set(r["positions"]) == {"B"}
    assert [(s["code"], s["reason"]) for s in r["skips"]] == [("A", reason)]


def test_skip_full_frozen_size_when_actual_debit_exceeds_remaining_cash():
    a = book.initial_account()
    a["cash"] = 501000.0
    p = plan(a, 0, [candidate("A"), candidate("B"), candidate("C")])
    assert p["buys"][0]["shares"] == 500
    assert p["skips"][0]["code"] == "B"
    r = book.execute_open(a, CAL[1], p, bars("A", opening=1010), CAL)
    assert not r["trades"]
    assert [(s["code"], s["reason"]) for s in r["skips"]] == [
        ("B", "insufficient_cash"),
        ("A", "insufficient_cash"),
    ]


def test_entry_day_stop_from_raw_open_fees_idempotent_and_no_lookahead():
    a = book.initial_account()
    p = plan(a, 0, [candidate("A")], stop_atr=1.5)
    untouched = deepcopy(p)
    first = book.execute_open(a, CAL[1], p, {"A": {"open": 1000}}, CAL)
    adversarial = book.execute_open(a, CAL[1], p, bars("A", low=1, close=1), CAL)
    assert first == adversarial
    assert not a["positions"] and p == untouched
    assert first["positions"]["A"]["stop"] == 970
    assert book.execute_open(first, CAL[1], p, {}, CAL) == first
    closed = book.execute_close(first, CAL[1], bars("A", low=970, close=995), CAL)
    assert book.execute_close(closed, CAL[1], {}, CAL) == closed
    assert book.execute_day(closed, CAL[1], p, {}, CAL) == closed
    buy, sell = closed["trades"]
    assert sell["raw_price"] == 970
    assert (sell["reason"], sell["holding_sessions"]) == ("intraday_stop", 1)
    assert sell["entry_date"] == sell["exit_date"] == CAL[1]
    assert sell["shares"] == buy["shares"] == 500
    net = 500 * (970 * 0.999 * 0.9995 - 1000 * 1.001 * 1.0005)
    assert sell["net_pnl"] == pytest.approx(net)
    assert closed["equity"][0]["nav"] == pytest.approx(3_000_000 + net)
    assert closed["equity"][0]["drawdown"] == pytest.approx(net / 3_000_000)
    assert not closed["positions"] and first["positions"]


@pytest.mark.parametrize(
    "event,opening,expected",
    [
        (True, 850, "earnings"),
        (False, 850, "gap_stop"),
        (False, 960, "gap_stop"),
        (False, 1002, "above_sma5"),
    ],
)
def test_earnings_then_gap_stop_then_planned_open_then_intraday_priority(event, opening, expected):
    a = buy_once("A")
    p = plan(
        a,
        1,
        marks={"A": {"close": 990, "sma5": 985}},
        earnings={"A": [CAL[2]] if event else []},
        allow_entries=False,
    )
    result = book.execute_day(a, CAL[2], p, bars("A", opening=opening, low=700), CAL)
    sale = result["trades"][-1]
    assert sale["reason"] == expected and sale["raw_price"] == opening
    assert sale["price"] == pytest.approx(opening * 0.999)
    assert len(result["trades"]) == 2


def test_t_plus_two_releases_net_receivable_at_open_even_if_signal_day_precedes_settlement():
    a = buy_once("A", low=950)
    sale = a["trades"][-1]
    credit = sale["shares"] * sale["price"] - sale["fee"]
    assert a["settlements"] == [dict(date=CAL[3], amount=pytest.approx(credit))]
    assert book.available_cash(a, CAL[2]) == pytest.approx(a["cash"] - credit)
    assert book.available_cash(a, CAL[3]) == a["cash"]
    p = plan(a, 1)
    a = book.execute_day(a, CAL[2], p, {}, CAL)
    a = book.execute_day(a, CAL[3], plan(a, 2), {}, CAL)
    assert not a["settlements"]
    assert a["equity"][-1]["unsettled"] == 0


def test_frozen_earnings_exit_includes_a_non_session_between_signal_and_order():
    a = buy_once("A")
    assert (CAL[1], CAL[2]) == ("2026-10-02", "2026-10-05")
    p = plan(a, 1, earnings={"A": ["2026-10-04"]}, allow_entries=False)
    assert p["sells"][0]["reason"] == "earnings"
    assert plan(a, 1, earnings={"A": [CAL[1]]})["sells"] == []


def test_replay_switch_bypasses_both_earnings_checks_and_preserves_other_exits():
    signals = [candidate("A"), candidate("B")]
    first = plan(
        book.initial_account(), 0, signals, earnings={"A": [CAL[1]]}, enable_earnings_guard=False
    )
    assert first["enable_earnings_guard"] is False
    assert [b["code"] for b in first["buys"]] == ["A", "B"]
    a = buy_once("A")
    a["positions"]["A"]["pending_exit"] = "earnings"
    opts = dict(earnings={"A": [CAL[2]]}, enable_earnings_guard=False)
    assert plan(a, 1, **opts)["sells"] == []
    assert (
        plan(a, 1, marks={"A": {"close": 990, "sma5": 985}}, **opts)["sells"][0]["reason"]
        == "above_sma5"
    )
    with pytest.raises(ValueError, match="invalid_enable_earnings_guard"):
        plan(a, 1, enable_earnings_guard=0)


def test_unknown_extra_announcement_protects_entries_and_preserves_a_known_exit():
    a = buy_once("A")
    declared = {"A": [None, CAL[2]], "B": [None, CAL[8]]}
    next_plan = plan(a, 1, [candidate("B")], earnings=declared)
    assert [r["reason"] for r in next_plan["sells"]] == ["earnings"]
    assert [r["reason"] for r in next_plan["skips"]] == ["unknown_earnings"]


def test_confirm_actual_sale_before_reusing_sector_slots_and_no_same_day_reentry():
    a = buy_once("A", "B")
    p = plan(a, 1, [candidate(c) for c in "ACD"], marks={"A": {"close": 990, "sma5": 985}})
    assert "A" not in {o["code"] for o in p["buys"]}
    failed = book.execute_open(a, CAL[2], p, {"A": {"no_trade": True}, **bars("B", "C", "D")}, CAL)
    assert set(failed["positions"]) == {"A", "B"}
    assert failed["positions"]["A"]["pending_exit"] == "above_sma5"
    success = book.execute_open(a, CAL[2], p, bars("A", "B", "C", "D"), CAL)
    assert set(success["positions"]) == {"B", "C"}
    assert any(s["code"] == "D" and s["reason"] == "sector_limit" for s in success["skips"])


def test_tenth_session_close_queues_sale_only_on_following_open():
    a = buy_once("A")
    for i in range(1, 10):
        p = plan(a, i, marks={"A": {"close": 990, "sma5": 990}}, allow_entries=False)
        assert not p["sells"]
        a = book.execute_day(a, CAL[i + 1], p, bars("A"), CAL)
    p = plan(a, 10, allow_entries=False)
    assert [sale["reason"] for sale in p["sells"]] == ["time_exit"]
    sale = book.execute_day(a, CAL[11], p, bars("A"), CAL)["trades"][-1]
    assert sale["reason"] == "time_exit" and sale["holding_sessions"] == 11


def test_verified_no_trade_preserves_prior_mark_and_pending_exit():
    a = buy_once("A", "B")
    p = plan(a, 1, marks={"A": {"close": 990, "sma5": 985}})
    result = book.execute_day(a, CAL[2], p, {"A": {"no_trade": True}, "B": {"no_trade": True}}, CAL)
    rec = result["equity"][-1]
    assert rec["no_trade_marks"] == ["A", "B"]
    assert rec["stale_marks"] == rec["unverified_stops"] == []
    assert rec["nav"] == a["equity"][-1]["nav"]
    assert result["positions"]["A"]["pending_exit"] == "above_sma5"
    assert len(result["trades"]) == 2


@pytest.mark.parametrize("missing", ["low", "close"])
def test_unverified_close_cannot_record_profit_and_retry_confirms_stop(missing):
    a = buy_once("A", "B")
    p = plan(a, 1, marks={"B": {"close": 990, "sma5": 985}})
    prices = bars("A", "B", opening=1000, low=980, close=1200)
    opened = book.execute_open(a, CAL[2], p, prices, CAL)
    frozen = deepcopy(opened)
    prices["A"][missing] = None
    for _ in range(2):
        with pytest.raises(book.C1UnverifiedBar) as exc:
            book.execute_close(opened, CAL[2], prices, CAL)
        assert exc.value.issues == {"A": [missing]}
        assert opened == frozen
    # B's confirmed opening sale survives the failed close. A's profit was never booked.
    assert opened["trades"][-1]["code"] == "B"
    assert opened["trades"][-1]["reason"] == "above_sma5"
    assert opened["last_date"] == CAL[1]
    prices["A"].update(low=950, close=1200)
    result = book.execute_close(opened, CAL[2], prices, CAL)
    assert result["trades"][-1]["raw_price"] == 960
    assert result["trades"][-1]["net_pnl"] < 0
    assert result["trades"][-1]["reason"] == "intraday_stop"
    assert book.execute_close(result, CAL[2], {}, CAL) == result


def test_missing_held_open_waits_and_retries_from_original_account():
    a = buy_once("A", "B")
    original = deepcopy(a)
    p = plan(a, 1, marks={"B": {"close": 990, "sma5": 985}})
    with pytest.raises(book.C1UnverifiedBar) as exc:
        book.execute_open(a, CAL[2], p, bars("B"), CAL)
    assert exc.value.issues == {"A": ["open"]}
    assert a == original
    prices = bars("A", "B")
    prices["A"]["open"] = 950
    result = book.execute_open(a, CAL[2], p, prices, CAL)
    assert [t["reason"] for t in result["trades"][-2:]] == ["gap_stop", "above_sma5"]
    assert not result["positions"]


def test_holding_split_preserves_basis_and_puts_stop_in_post_split_units():
    a = buy_once("A")
    p = plan(a, 1)
    b = {"A": {"open": 500, "low": 480, "close": 510, "adj_factor": 0.5, "ex_rights": "1"}}
    result = book.execute_day(a, CAL[2], p, b, CAL)
    sale = result["trades"][-1]
    assert (sale["raw_price"], sale["shares"], sale["reason"]) == (480, 1000, "intraday_stop")
    assert sale["cost"] == a["positions"]["A"]["cost"]
    b["A"]["adj_factor"] = 3
    with pytest.raises(book.C1UnverifiedBar, match="fractional corporate action"):
        book.execute_day(a, CAL[2], p, b, CAL)
    assert a["positions"]["A"]["shares"] == 500


def test_position_limit_and_balance_reconcile():
    a = book.initial_account()
    for i, rows in enumerate([("A", "B"), ("C", "D"), ("E", "F")]):
        ranked = [candidate(c, sector=c, close=1 if c == "F" else 850, atr=150) for c in rows]
        p = plan(a, i, ranked)
        prices = bars(*set(a["positions"]).union(rows), opening=850, low=800, close=900)
        a = book.execute_day(a, CAL[i + 1], p, prices, CAL)
    assert len(a["positions"]) == 5
    assert a["skips"][-1]["reason"] == "position_limit"
    nav = a["equity"][-1]["nav"]
    pnl = a["equity"][-1]["realized_pnl"] + a["equity"][-1]["unrealized_pnl"]
    assert nav == pytest.approx(a["capital"] + pnl)
    assert len({(r["date"], r["code"], r["side"]) for r in a["trades"]}) == len(a["trades"])


def test_cannot_freeze_or_execute_a_wrong_session_or_unfinished_account():
    a = book.initial_account()
    with pytest.raises(ValueError, match="next trading session"):
        book.freeze_plan(a, CAL[0], CAL[2], [], {}, {}, CAL)
    opened = book.execute_open(a, CAL[1], plan(a, 0), {}, CAL)
    with pytest.raises(ValueError, match="closed account"):
        plan(opened, 1)
    with pytest.raises(ValueError, match="open before"):
        book.execute_close(a, CAL[1], {}, CAL)
    with pytest.raises(ValueError, match="prior session"):
        book.execute_open(opened, CAL[2], plan(a, 1), {}, CAL)
