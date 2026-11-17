from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading_system.research.daytrade_universe import rank
from trading_system.research.swing_screen import features, paper, prepare
from trading_system.research.swing_screen.inputs import settings
from trading_system.research.swing_screen.portfolio import Book, purchase_bar, session
from trading_system.research.swing_screen.study import Cohort


@pytest.fixture
def cfg():
    return settings(Path(__file__).parents[1])


def synthetic():
    dates = list(pd.bdate_range("2024-01-02", periods=75).strftime("%Y-%m-%d"))
    rows = []
    for i, d in enumerate(dates):
        for j in range(35):
            if (j == 0 and i >= 68) or (j == 1 and i < 35):
                continue
            code = f"{1000 + j}0"
            half = 0.5 if j == 2 and i >= 44 else 1
            close = (500 + 3 * i + j) * half
            rows.append(
                dict(
                    Date=d,
                    Code=code,
                    CoName=code,
                    Mkt="0111",
                    ProdCat="011",
                    S33=str(j % 4),
                    S33Nm="s",
                    MktNm="m",
                    O=close,
                    H=close * 1.015,
                    L=close * 0.985,
                    C=close,
                    Va=(1e8 + 1000 * j),
                    Vo=3e5 / half,
                    UL=0,
                    LL=0,
                    AdjFactor=0.5 if j == 2 and i == 44 else 1.0,
                    ExRT="1" if j == 2 and i == 44 else "",
                )
            )
    index = pd.DataFrame(
        {"O": 1000.0, "H": 1010.0, "L": 990.0, "C": np.arange(75) + 1000},
        index=pd.Index(dates, name="Date"),
    )
    return dates, pd.DataFrame(rows), index


def test_future_universe_split_and_short_history(cfg):
    dates, raw, ix = synthetic()
    day = dates[64]
    a = features.Features(raw, ix, dates)
    full = a.day(day)
    later = raw.copy()
    later.loc[later.Date > day, "C"] = 1e7
    ix2 = ix.copy()
    ix2.loc[ix2.index > day, "C"] = 1e8
    other = features.Features(later, ix2, dates).day(day)
    pd.testing.assert_frame_equal(full, other)
    assert "10000" in full.index and "10000" not in a.day(dates[70]).index
    assert "10010" not in a.day(dates[10]).index and pd.notna(full.at["10010", "m20"])
    assert pd.isna(full.at["10010", "m60"])
    assert full.at["10020", "m60"] == pytest.approx((500 + 3 * 64 + 2) / (500 + 3 * 4 + 2) - 1)
    picks = features.rankings(full, cfg, day)
    assert picks == features.rankings(full.sample(frac=1, random_state=5), cfg, day)
    assert set(picks["B"]) == set(picks["C"])


def test_native_a_and_control(cfg):
    dates, raw, ix = synthetic()
    target = dates[-1]
    rows = raw.to_dict("records")
    master = {r["Code"]: r for r in rows if r["Date"] == target}
    master["72030"] = dict(master["10020"], Code="72030")
    series = {c: [r for r in rows if r["Code"] == c and r["Date"] in dates[-5:]] for c in master}
    base, _ = rank(series, master, dates[-5:])
    f = features.Features(raw, ix, dates).day(target)
    selected = features.rankings(f, cfg, target)
    assert selected["A"] == [r["code"] for r in base]
    assert len(set(selected["matched"])) == len(selected["B"])


def bar(px=500, **updates):
    return SimpleNamespace(
        **dict(
            dict(
                C=px,
                O=px,
                H=px * 1.02,
                L=px * 0.98,
                LL=0,
                UL=0,
                AdjFactor=1,
                ExRT="",
                S33="10",
                ma20=px,
                atr=10,
                m5=0.02,
            ),
            **updates,
        )
    )


def test_money_lot_fees_and_settlement(cfg):
    b = Book.start(100000)
    cfg = dict(cfg, position_fraction=1)
    q = {"A": bar(), "B": bar()}
    _, fills, _ = session(b, 0, "d", q, q, ["A", "B"], cfg, {"hold": 1})
    assert [(f["qty"], f["fee"]) for f in fills] == [(100, 35)]
    assert b.cash == 49965
    _, fills, skips = session(b, 1, "d", q, q, ["B"], cfg, {"hold": 1})
    assert fills[0]["pnl"] == -70 and b.power(1) == 49965 and skips["cash_or_position_budget"]
    assert b.power(2) == 49965 and b.power(3) == 99930
    assert Book.restore(b.dump()).cash == b.cash


def test_unfilled_gap_and_rights(cfg):
    b = Book.start(1000000)
    q = {"A": bar()}
    assert purchase_bar(bar(600, L=495), "limit", 495)[0] is None
    assert purchase_bar(bar(600, L=490), "limit", 495)[0] == 495
    assert purchase_bar(bar(600, H=600, L=600, UL=1), "open", 0)[0] is None
    assert purchase_bar(None, "open", 0)[0] is None
    session(b, 0, "a", q, q, ["A"], cfg, {"hold": 5, "exit": "stop"})
    _, fills, _ = session(b, 1, "b", q, {"A": bar(450)}, [], cfg, {"hold": 5, "exit": "stop"})
    assert fills[0]["price"] == 450 and fills[0]["reason"] == "stop_gap"
    b = Book.start(1000000)
    session(b, 0, "a", q, q, ["A"], cfg, {"hold": 1})
    r, fs, _ = session(b, 1, "b", q, {"A": bar(250, AdjFactor=0.5, ExRT="1")}, [], cfg, {"hold": 1})
    assert abs(fs[0]["pnl"] + fs[0]["fee"] * 2) < 0.01 and r["uncertain"] is False
    b = Book.start(1000000)
    session(b, 0, "a", q, q, ["A"], cfg, {"hold": 5})
    r, fs, k = session(b, 1, "b", q, {}, [], cfg, {"hold": 5})
    assert r["uncertain"] and k["unpriced_holding"] and not fs


def test_labels_and_price_only(cfg):
    q = {"A": bar()}
    cfg = dict(cfg, holds=[1])
    c = Cohort("day", 0, {"B": ["A"]}, q, 100, q, "up", cfg)
    rows = c.step(1, {"A": bar(255, AdjFactor=0.5, ExRT="1")}, 101)
    b = next(x for x in rows if x["method"] == "B")
    assert b["gross"] == pytest.approx(0.02)
    assert b["net"] == pytest.approx((1000 - 20 - 50.5) / 50035)
    assert b["excess"] == pytest.approx(b["net"] - 0.01)


def test_version_freeze_and_forward_boundary(tmp_path, cfg, monkeypatch):
    old = prepare.root(tmp_path) / "daily/2026-11-16/first.json"
    prepare.write(old, dict(state="ready", version="frozen", target="2026-11-17"))
    assert prepare.daily(tmp_path, "2026-11-16", cfg=cfg)["version"] == "frozen"
    assert not (prepare.root(tmp_path) / "paper").exists()

    class Fake:
        def __init__(self, *a):
            self.receipts = {}

        def calendar(self, *a):
            return ["2026-11-16", "2026-11-17", "2026-11-18"]

    monkeypatch.setattr(prepare, "Sources", Fake)
    assert prepare.daily(tmp_path, "2026-11-15", cfg=cfg)["state"] == "holiday"
    assert (
        prepare.daily(
            tmp_path, "2026-11-17", cfg=cfg, now=datetime(2026, 11, 17, 0, 10, tzinfo=UTC)
        )["state"]
        == "outside_publication_window"
    )
    assert (
        prepare.daily(tmp_path, "2026-11-17", cfg=cfg, now=datetime(2026, 11, 17, 9, tzinfo=UTC))[
            "state"
        ]
        == "awaiting_daily_receipt"
    )
    monkeypatch.setattr(paper, "settings", lambda p: cfg)
    assert paper.settle(tmp_path, "2026-11-16")["state"] == "awaiting_first_open"
    assert not list(tmp_path.rglob("*.sqlite"))


def test_view_import_has_no_broker_or_ledger(tmp_path):
    import ast

    from trading_system.research.swing_screen import view

    tree = ast.parse(Path(view.__file__).read_text(encoding="utf-8"))
    assert not any(
        "broker" in str(n.module) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
    )
    assert paper.after_daily(tmp_path, "2026-11-17") is None


def test_events_disclosure_boundary():
    dates, raw, ix = synthetic()
    f = features.Features(raw, ix, dates).day(dates[50])
    financial = pd.DataFrame(
        [
            dict(DiscDate=dates[i], DiscNo=str(i), Code="10020", DocType="FinancialStatements")
            for i in (30, 60)
        ]
    )
    schedule = pd.DataFrame(
        [dict(PubDate=dates[i], SchDate=dates[70], Code="10020") for i in (52, 53)]
    )
    out = prepare.events(f, dates[50], financial, schedule)
    assert out.loc["10020", "last_earnings"] == dates[30] and pd.isna(
        out.loc["10020", "next_earnings"]
    )


def test_daily_to_paper_freeze_revision_and_replay(tmp_path, cfg, monkeypatch):
    dates, raw, index = synthetic()
    decision, target, following = dates[68:71]
    now = datetime.fromisoformat(decision + "T09:00:00+00:00")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    class Fake:
        daily = SimpleNamespace(cache_clear=lambda: None)
        edition = 1

        def __init__(self, *args):
            self.receipts = {d: True for d in dates}
            self.refs = {"edition": self.edition}

        def calendar(self, *args):
            return dates

        def blocks(self, session_dates):
            return raw.loc[raw.Date.isin(session_dates)].copy(), index.loc[session_dates].copy()

        def table(self, name, *args):
            cols = {"financial": "DiscDate Code DiscNo DocType", "schedule": "PubDate Code SchDate"}
            return pd.DataFrame(columns=cols[name].split())

    monkeypatch.setattr(prepare, "datetime", Clock)
    monkeypatch.setattr(prepare, "Sources", Fake)
    monkeypatch.setattr(paper, "Sources", Fake)
    monkeypatch.setattr(paper, "settings", lambda project: cfg)
    home = prepare.root(tmp_path)
    frozen = prepare.daily(tmp_path, decision, cfg=cfg, now=now)
    assert frozen["mode"] == "forward" and frozen["target"] == target
    frame = pd.read_parquet(home / "daily" / decision / frozen["version"] / "features.parquet")
    assert (
        frame.eligible_rank_B.dropna().sort_values().index[:30].tolist()
        == frozen["candidates"]["B"]
    )
    file = home / "daily" / decision / "first.json"
    original = file.read_bytes()
    # A correction known only after the opening can never replace the first version.
    Fake.edition = 2
    now = datetime.fromisoformat(target + "T09:00:00+00:00")
    newer = prepare.daily(tmp_path, decision, revision=True, cfg=cfg, now=now)
    assert newer["version"] != frozen["version"] and file.read_bytes() == original
    assert paper.settle(tmp_path, target, now=now)["accounts"] == 48
    a = prepare.load(home / "paper" / (target + ".json"))
    assert a["selection"] == frozen["version"]
    assert any(v["fills"] for v in a["accounts"].values())
    assert all(f["qty"] % 100 == 0 for v in a["accounts"].values() for f in v["fills"])
    ledger = (home / "paper" / (target + ".json")).read_bytes()
    now = datetime.fromisoformat(following + "T09:00:00+00:00")
    assert paper.settle(tmp_path, following, now=now)["accounts"] == 48
    b = prepare.load(home / "paper" / (following + ".json"))
    assert b["missing_selection"] and not any(v["fills"] for v in b["accounts"].values())
    assert (home / "paper" / (target + ".json")).read_bytes() == ledger
    assert not list(tmp_path.rglob("*.sqlite"))


def test_earnings_calendar_and_rights(tmp_path):
    from trading_system.research.swing_screen.events import EventClock

    pd.DataFrame(
        [dict(DiscDate="2026-11-14", Code="X", DiscNo="1", DocType="FinancialStatements")]
    ).to_parquet(tmp_path / "financial.parquet")
    pd.DataFrame([dict(PubDate="2026-11-12", Code="X", SchDate="2026-11-17")]).to_parquet(
        tmp_path / "schedule.parquet"
    )
    events = EventClock(tmp_path)
    cal = [
        "2026-11-13",
        "2026-11-16",
        "2026-11-17",
        "2026-11-18",
        "2026-11-19",
        "2026-11-20",
        "2026-11-24",
    ]
    before = {}
    for i, d in enumerate(cal):
        mk = {"X": bar(500 + i, ExRT="3" if i == 3 else "")}
        ranks = {m: ["X"] for m in "ABCD"}
        events.screen(d, i, cal, mk, before, ranks)
        if i == 0:
            assert ranks["D_before_earnings"] == ["X"]
        if i == 2:
            assert ranks["D_after_earnings"] == ["X"] and ranks["D_before_earnings"] == []
        if i >= 3:
            assert ranks["D_after_earnings"] == []
        before = mk
    assert [r["after_return"] is not None for r in events.labels] == [True, False]
