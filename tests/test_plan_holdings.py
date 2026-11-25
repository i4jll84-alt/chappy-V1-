import hashlib
import json
from datetime import datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest

from trading_system import plan_holdings as h
from trading_system.research import a0_5_book as ledger


def now(stamp="2026-11-20T20:30:00+09:00"):
    return datetime.fromisoformat(stamp)


def receipt(project, day, state="complete"):
    path = project / "data/jquants/daily-snapshots" / day / "last_collection.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(date=day, state=state)), encoding="utf-8")
    return path


def market(monkeypatch):
    def read(project, days, asof):
        assert asof.utcoffset() is not None
        return (
            {
                "12340": [dict(Date=days[0], C="550.5")],
                "56780": [dict(Date=days[0], C="1020.5")],
            },
            {"12340": {"CoName": "銘柄A"}, "56780": {"CoName": "銘柄B"}},
            [],
        )

    mock = Mock(side_effect=read)
    monkeypatch.setattr(h, "dated_inputs", mock)
    return mock


def a_plan():
    return dict(
        last_mark="2026-11-20",
        positions={
            "12340": dict(
                quote="1000", entry="500", shares="200", debit="100060", entered="2026-11-19"
            )
        },
    )


def b_plan(project):
    root = project / "data/a0-5-paper/accounts/3000000"
    state = ledger.initial(3_000_000)
    state["positions"] = [dict(Code="56780", entry_date="2026-11-19", shares=200, cost=100150.05)]
    state["journal"] = [
        dict(
            Code="56780", side="BUY", date="2026-11-19", status="仮想約定", price=1001, shares=100
        ),
        dict(Code="56780", side="BUY", date="2026-11-18", status="仮想約定", price=900, shares=100),
    ]
    ledger.save(root, state)
    plan = dict(
        capital=3_000_000, native_revision=state["revision"], positions=1, last_mark="2026-11-20"
    )
    state["positions"] = []  # A newer native revision must not leak into an older summary.
    ledger.save(root, state)
    return plan, root / "ledger.sqlite"


def test_matches_both_ledgers_and_uses_original_fill_price_without_rewriting(tmp_path, monkeypatch):
    receipt(tmp_path, "2026-11-20")
    market(monkeypatch)
    b, db = b_plan(tmp_path)
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    result = h.holdings_snapshot(tmp_path, {"A": a_plan(), "B": b}, now())
    a_row, b_row = result["rows"]
    assert (a_row["name"], a_row["entry_price"], a_row["current_price"]) == (
        "銘柄A",
        Decimal("1000"),
        Decimal("550.5"),
    )
    assert (b_row["name"], b_row["entry_price"], b_row["shares"]) == (
        "銘柄B",
        Decimal("1001"),
        Decimal("200"),
    )
    assert all("分割・併合" in row["note"] for row in result["rows"])
    assert all(row["price_date"] == "2026-11-20" for row in result["rows"])
    # Splits preserve total acquisition cost; the original quote is not today's unit cost.
    assert a_row["net"] == Decimal("10040")
    assert a_row["return_pct"] == Decimal("10040") / Decimal("100060") * 100
    assert b_row["net"] == Decimal("103949.95")
    assert b_row["return_pct"] == Decimal("103949.95") / Decimal("100150.05") * 100
    assert hashlib.sha256(db.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("kind", ["missing_price", "missing_entry"])
def test_missing_values_are_not_replaced_with_cost_basis(tmp_path, monkeypatch, kind):
    receipt(tmp_path, "2026-11-20")
    market(monkeypatch)
    plan = a_plan()
    if kind == "missing_price":
        monkeypatch.setattr(h, "dated_inputs", lambda *args: ({"12340": [{"C": None}]}, {}, []))
    else:
        del plan["positions"]["12340"]["quote"]
    result = h.holdings_snapshot(tmp_path, {"A": plan, "B": {"positions": 0}}, now())
    row = result["rows"][0]
    assert row["current_price" if kind == "missing_price" else "entry_price"] is None
    assert "終値欠損" in row["note"] if kind == "missing_price" else "取得価格未確認" in row["note"]
    assert "B：保有0銘柄" in result["status"]


def test_one_unavailable_account_does_not_hide_the_other(tmp_path, monkeypatch):
    receipt(tmp_path, "2026-11-20")
    market(monkeypatch)
    b, db = b_plan(tmp_path)
    b["native_revision"] = 999
    result = h.holdings_snapshot(tmp_path, {"A": a_plan(), "B": b}, now())
    assert [row["plan"] for row in result["rows"]] == ["A"]
    assert "B：保有記録を確認できません" in result["status"]


@pytest.mark.parametrize(
    "stamp,expected",
    [("2026-11-20T10:00:00+09:00", "2026-11-19"), ("2026-11-20T20:00:00+09:00", "2026-11-20")],
)
def test_future_and_unfinished_intraday_prices_are_excluded(tmp_path, monkeypatch, stamp, expected):
    for day in ["2026-11-19", "2026-11-20", "2026-11-24"]:
        receipt(tmp_path, day)
    mock = market(monkeypatch)
    _, _, day, issue = h.saved_prices(tmp_path, now(stamp))
    assert day == expected and not issue
    mock.assert_called_once_with(tmp_path / "data/jquants", [expected], now(stamp))


def test_partial_snapshot_is_labelled_and_verification_failure_does_not_fall_back(
    tmp_path, monkeypatch
):
    receipt(tmp_path, "2026-11-19")
    receipt(tmp_path, "2026-11-20", "running")
    mock = market(monkeypatch)
    _, _, day, issue = h.saved_prices(tmp_path, now())
    assert day == "2026-11-19" and "未完了" in issue
    receipt(tmp_path, "2026-11-20")
    mock.side_effect = ValueError("prices_not_yet_available")
    quotes, names, day, issue = h.saved_prices(tmp_path, now())
    assert not quotes and not names and day is None and "確認できません" in issue


def test_cache_refreshes_on_saved_price_poll_or_account_change(tmp_path, monkeypatch):
    tick = [10]
    monkeypatch.setattr(h, "monotonic", lambda: tick[0])
    load = Mock(return_value=dict(rows=[]))
    monkeypatch.setattr(h, "holdings_snapshot", load)
    reader = h.HoldingsReader(tmp_path)
    plans = {"A": a_plan()}
    reader.read(plans, now())
    reader.read(plans, now())
    assert load.call_count == 1
    tick[0] = 40
    reader.read(plans, now())
    assert load.call_count == 2
    plans["A"]["positions"] = {}
    reader.read(plans, now())
    assert load.call_count == 3


def test_fixed_term_holdings_use_each_accounts_cost_and_shares(tmp_path, monkeypatch):
    receipt(tmp_path, "2026-11-20")
    read_prices = market(monkeypatch)
    nine = dict(a_plan(), adapter="plan_a_forward")
    ten = dict(a_plan(), adapter="plan_a_forward")
    ten["positions"]["12340"].update(shares="100", debit="54000")
    result = h.holdings_snapshot(tmp_path, {"A10": ten, "A": {"positions": {}}, "A9": nine}, now())
    assert [(r["plan"], r["shares"], r["net"]) for r in result["rows"]] == [
        ("A9", Decimal(200), Decimal(10040)),
        ("A10", Decimal(100), Decimal(1050)),
    ]
    assert "A：保有0銘柄" in result["status"]
    read_prices.assert_called_once()


@pytest.mark.parametrize("missing", ["debit", "shares", "mark", "older_mark", "future_mark"])
def test_holdings_profit_needs_verified_cost_and_matching_share_price_dates(
    tmp_path, monkeypatch, missing
):
    receipt(tmp_path, "2026-11-20")
    market(monkeypatch)
    plan = a_plan()
    if missing in ("debit", "shares"):
        del plan["positions"]["12340"][missing]
    else:
        plan["last_mark"] = {"mark": None, "older_mark": "2026-11-19", "future_mark": "2026-11-24"}[
            missing
        ]
    row = h.holdings_snapshot(tmp_path, {"A": plan}, now())["rows"][0]
    assert row["net"] is None and row["return_pct"] is None
    assert "未確認" in row["note"]
    assert row["current_price"] == Decimal("550.5")
