from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_system.research import a0_5 as native
from trading_system.research import a0_5_book as ledger
from trading_system.research import plan_b_forward as plan


@pytest.fixture
def comparison(tmp_path, monkeypatch):
    calendar = pd.to_datetime(["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-24"])
    monkeypatch.setattr(native.inputs, "calendar", lambda *args: calendar)
    project = tmp_path / "project"
    book = project / "new-plan-b"
    capital = 3_000_000
    root = native.root_for(project, capital)
    state = ledger.initial(capital)
    state["last_open"] = "2026-11-18"
    state["plans"]["2026-11-18"] = {
        "state": "ready",
        "date": "2026-11-18",
        "order_date": "2026-11-19",
        "orders": [],
        "candidates": [],
        "equity": capital,
    }
    ledger.save(root, state)
    (native.root_for(project) / "enabled").touch()

    def tick(when):
        return plan.advance(
            project,
            book,
            start="2026-11-19",
            capital=capital,
            now=datetime.fromisoformat(when + "+09:00"),
        )

    return SimpleNamespace(state=state, root=root, book=book, project=project, tick=tick)


def test_starts_from_verified_cash_and_excludes_prior_decisions(comparison):
    c = comparison
    first = c.tick("2026-11-19T00:35")
    stamp = (c.book / "baseline.json").read_bytes()
    assert first["status"] == "waiting_for_first_close"
    assert first["first_entry"] == "2026-11-20"
    assert first["nav"] == first["cash"] == 3_000_000
    assert first["last_decision"] is None and first["last_mark"] is None
    assert first["orders"] is None and first["fills"] == 0
    for when, state in (
        ("2026-11-19T17:05", "awaiting_native_decision"),
        ("2026-11-20T07:55", "missing_native_decision"),
    ):
        assert c.tick(when)["status"] == state
    assert (c.book / "baseline.json").read_bytes() == stamp
    assert ledger.load(c.root, 3_000_000)["revision"] == 1


def test_records_actual_native_mark_and_fills_once(comparison):
    c = comparison
    c.tick("2026-11-19T00:35")
    d = dict(
        state="ready",
        date="2026-11-19",
        order_date="2026-11-20",
        orders=[{"Code": "12340"}],
        candidates=[{"Code": "12340"}],
        equity=3_000_000,
    )
    c.state["plans"][d["date"]] = d
    ledger.save(c.root, c.state)
    r = c.tick("2026-11-19T20:10")
    assert r["last_decision"] == r["last_mark"] == d["date"]
    assert r["status"] == "ready" and r["orders"] == 1 and r["next_entry"] == "2026-11-20"
    c.state["journal"].append(dict(date="2026-11-20", status="仮想約定"))
    c.state["positions"] = [{"Code": "12340"}]
    c.state["cash"] = 2_900_000
    c.state["plans"]["2026-11-20"] = dict(
        d, date="2026-11-20", equity=3_001_000, order_date="2026-11-24"
    )
    ledger.save(c.root, c.state)
    r = c.tick("2026-11-20T20:10")
    assert (r["nav"], r["cash"], r["positions"], r["fills"]) == (3_001_000, 2_900_000, 1, 1)
    assert c.tick("2026-11-20T20:11") == r


def test_rejects_dirty_or_late_baseline_and_detects_ledger_replacement(comparison):
    c = comparison
    c.state["plans"]["2026-11-18"]["orders"] = [{"Code": "12340"}]
    ledger.save(c.root, c.state)
    with pytest.raises(ValueError, match="既存"):
        c.tick("2026-11-19T08:30")
    assert not (c.book / "baseline.json").exists()
    c.state["plans"]["2026-11-18"]["orders"] = []
    ledger.save(c.root, c.state)
    with pytest.raises(ValueError, match="引け前"):
        c.tick("2026-11-19T16:00")
    c.tick("2026-11-19T08:30")
    (c.root / "ledger.sqlite").unlink()
    ledger.save(c.root, ledger.initial(3_000_000))
    with pytest.raises(ValueError, match="台帳"):
        c.tick("2026-11-19T16:00")
