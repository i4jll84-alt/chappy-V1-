import json
from datetime import date, datetime, timedelta

import pytest

from trading_system.research import daytrade_universe as u


def test_rank_uses_turnover_then_typical_range_and_keeps_control_separate():
    days = ["01", "02", "03", "04", "05"]
    master = {}
    histories = {}
    for i in range(110):
        code = f"{2000 + i}0"
        master[code] = dict(Mkt="0111", ProdCat="011", CoName=code, MktNm="A", S33Nm="B")
        histories[code] = [
            dict(Date=d, O=100, H=100 + i, L=100, C=100, Vo=1, Va=111 - i, AdjFactor=1, ExRT=None)
            for d in days
        ]
    master[u.CONTROL] = {}
    histories["20000"][0]["Va"] = None
    histories["20010"][0]["ExRT"] = "1"
    chosen, counts = u.rank(histories, master, days)
    assert counts["eligible"] == 108 and counts["liquidity_shortlist"] == 100
    assert [r["code"] for r in chosen] == [f"{2000 + i}0" for i in range(101, 71, -1)]
    assert counts["missing_price_or_turnover"] == counts["corporate_action"] == 1


def test_only_use_exact_day_and_no_preview_or_stale_fallback(tmp_path):
    day = date(2026, 11, 16)
    root = tmp_path / "data/rss-panel"
    (tmp_path / "config").mkdir()
    (tmp_path / "config/daytrade-universe.json").write_text(json.dumps({"from_session": str(day)}))
    assert u.panel_location(root, day - timedelta(days=3)) == root
    with pytest.raises(ValueError, match="daily_panel_missing"):
        u.panel_location(root, day)
    folder = root / "daily" / str(day)
    folder.mkdir(parents=True)
    sample = [{"code": f"{2000 + i}0"} for i in range(30)]
    info = dict(
        target_session=str(day),
        source_date="2026-11-13",
        selected_at="2026-11-13T18:00:00+09:00",
        published_at="2026-11-13T18:01:00+09:00",
        selected=sample,
        preview=False,
    )
    symbols = ["7203.T", *[r["code"][:4] + ".T" for r in sample]]
    (folder / "panel.json").write_text(
        json.dumps(dict(version=1, session=str(day), symbols=symbols))
    )
    (folder / "selection.json").write_text(json.dumps(info))
    assert u.panel_location(root, day) == folder
    for change in (
        {"preview": True},
        {"published_at": "2026-11-16T09:00:00+09:00"},
        {"selected_at": "2026-11-16T09:00:00+09:00"},
        {"source_date": str(day)},
        {"selected": sample[::-1]},
    ):
        (folder / "selection.json").write_text(json.dumps({**info, **change}))
        with pytest.raises(ValueError, match="invalid_panel_configuration"):
            u.panel_location(root, day)


def test_calendar_handles_weekend_holiday_and_missing_dates(monkeypatch):
    monday = date(2026, 11, 23)
    calendar = {
        str(monday + timedelta(days=i)): "1"
        if (monday + timedelta(days=i)).weekday() < 5 and i != 0
        else "0"
        for i in range(-15, 5)
    }
    monkeypatch.setattr(u, "calendar_days", lambda *_: (calendar, []))
    asof = datetime(2026, 11, 24, 8, tzinfo=u.JST)
    friday = date(2026, 11, 20)
    assert u.latest_closed_session(None, asof) == friday
    days, target, _ = u.window(None, friday, asof)
    assert len(days) == 5 and target == date(2026, 11, 24)
    del calendar[str(monday)]
    with pytest.raises(ValueError, match="incomplete_exchange_calendar"):
        u.window(None, friday, asof)
