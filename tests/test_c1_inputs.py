import json
from datetime import datetime

import pandas as pd
import pytest

from trading_system.research import c1_inputs as src


def cutoff(text="2026-11-20T22:00:00+09:00"):
    return datetime.fromisoformat(text)


def observation(day, value, available, **kwargs):
    return dict(
        source="VIX",
        date=day,
        close=value,
        final=True,
        published_at="2026-11-19T17:00:00-05:00",
        available_at=available,
        source_ref="tested-source",
        **kwargs,
    )


def test_prior_exchange_session_and_revision_by_cutoff():
    now = cutoff()
    receipt = "2026-11-20T08:00:00+09:00"
    nov19, nov20 = "2026-11-19", "2026-11-20"
    document = dict(
        observations=[
            observation(nov19, 30, receipt),
            observation(nov19, 10, "2026-11-21T00:01:00+09:00"),
            observation(nov20, 13, receipt),
        ],
        calendar=[
            dict(
                source="VIX",
                date=nov19,
                is_session=True,
                available_at=receipt,
                source_ref="US calendar",
            )
        ],
    )
    refs = []
    assert src._volatility(document, "VIX", nov20, [], now, refs) == 30
    assert refs[-1]["date"] == nov19 and refs[-1]["source_ref"] == "tested-source"
    document["calendar"][0]["is_session"] = False
    assert src._volatility(document, "VIX", nov20, [], now, []) is None
    document["calendar"].append(
        dict(
            source="VIX",
            date="2026-11-18",
            is_session=True,
            available_at=receipt,
            source_ref="US calendar",
        )
    )
    document["observations"].append(observation("2026-11-18", 23, receipt))
    assert src._volatility(document, "VIX", nov20, [], now, []) == 23
    assert src._volatility(document, "NIKKEI_VI", nov20, [nov19, nov20], now, []) is None


def test_earnings_unknown_and_explicit_none_frozen_at_cutoff(tmp_path, monkeypatch):
    available = "2026-11-20T16:00:00+09:00"
    evidence = dict(
        state="verified", manifest="stub", manifest_sha256="abc", available_at=available
    )
    monkeypatch.setattr(
        src.swing_earnings,
        "asof",
        lambda *_: (
            evidence,
            {
                "10000": [["2026-11-24", "A"]],
                "10010": [[None, "unknown"]],
                "10020": [["2026-01-01", "old"]],
            },
        ),
    )
    p = tmp_path / "data/c1-inputs/earnings.json"
    p.parent.mkdir(parents=True)
    base = dict(
        signal_date="2026-11-20",
        published_at=available,
        available_at=available,
        source_ref="company notice",
    )
    p.write_text(
        json.dumps(
            dict(
                observations=[
                    dict(base, code="10030", dates=[]),
                    dict(
                        base,
                        code="10030",
                        dates=["2026-11-25"],
                        available_at="2026-11-21T06:00:00+09:00",
                    ),
                ]
            )
        ),
        encoding="utf-8",
    )
    refs = []
    assert src._earnings(tmp_path, "2026-11-20", cutoff(), refs) == {
        "10000": ["2026-11-24"],
        "10030": [],
    }
    assert len(refs) == 2


def test_future_and_incomplete_bars_never_create_official_open(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="official_daily_bars_not_available"):
        src.load_bars(tmp_path, "2026-11-20", cutoff("2026-11-20T10:00:00+09:00"))
    with pytest.raises(ValueError, match="official_daily_bars_missing"):
        src.load_bars(tmp_path, "2026-11-20", cutoff())
    p = tmp_path / "data/jquants/daily-snapshots/2026-11-20/last_collection.json"
    p.parent.mkdir(parents=True)
    p.write_text(
        json.dumps(
            dict(date="2026-11-20", completed_at="2026-11-20T23:00:00+09:00", state="complete")
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(src, "dated_inputs", lambda *_: pytest.fail("read a future snapshot"))
    with pytest.raises(ValueError, match="official_daily_bars_missing"):
        src.load_bars(tmp_path, "2026-11-20", cutoff())


def test_missing_archive_marks_from_daily_receipt_and_future_topix_block_entries(
    tmp_path, monkeypatch
):
    day = "2026-11-20"
    days = pd.bdate_range(end=day, periods=75).append(pd.bdate_range(start="2026-11-24", periods=4))
    monkeypatch.setattr(src.saved, "calendar", lambda *args: days)
    row = dict(
        Date=day, Code="10000", C=100, O=101, H=105, L=98, Vo=500, Va=50000, AdjFactor=1, ExRT=None
    )
    master = dict(Date=day, Code="10000", Mkt="0111", S33="3650", ProdCat="011")
    monkeypatch.setattr(src, "_receipt", lambda root, d, cut: {} if d == day else None)
    monkeypatch.setattr(
        src, "dated_inputs", lambda *args: ({"10000": [row]}, {"10000": master}, [])
    )

    def missing_topix(*_):
        raise ValueError("not_available_at_cutoff")

    monkeypatch.setattr(src.saved, "market_pages", missing_topix)
    result = src.load_day(tmp_path, day, cutoff())
    assert result["marks"] == {"10000": {"close": 100, "sma5": None}}
    assert not result["coverage_ok"] and result["ranked"] == []
    assert any(x.startswith("topix_unverified") for x in result["reasons"])
    assert src.load_bars(tmp_path, day, cutoff())["10000"] == dict(
        open=101,
        high=105,
        low=98,
        close=100,
        adj_factor=1,
        ex_rights=None,
        no_trade=False,
    )


@pytest.mark.parametrize(
    "volume,opening,expected",
    [(0, None, True), (None, None, False), (0, "bad", False), (0, 2, False)],
)
def test_official_no_trade_requires_explicit_zero_volume_and_four_null_prices(
    tmp_path, monkeypatch, volume, opening, expected
):
    day = "2026-11-20"
    monkeypatch.setattr(src, "_receipt", lambda *_: {})
    row = dict(Date=day, Code="10000", O=opening, H=None, L=None, C=None, Vo=volume)
    monkeypatch.setattr(src, "dated_inputs", lambda *_: ({"10000": [row]}, {}, []))
    assert src.load_bars(tmp_path, day, cutoff())["10000"]["no_trade"] is expected


def test_absent_calendar_does_not_invent_historical_days_or_entries(tmp_path):
    result = src.load_day(tmp_path, "2026-11-20", cutoff())
    assert not result["allow_entries"] and not result["coverage_ok"]
    assert result["ranked"] == [] and result["marks"] == {}
    assert any(x.startswith("calendar_unavailable") for x in result["reasons"])
    with pytest.raises(ValueError, match="signal_close_in_future"):
        src.load_day(tmp_path, "2026-11-20", cutoff("2026-11-20T09:00:00+09:00"))


def test_current_complete_daily_list_replaces_archive_cross_section():
    old = pd.DataFrame(
        {"Date": ["2026-11-19", "2026-11-20", "2026-11-20"], "Code": ["A", "A", "B"]}
    )
    new = pd.DataFrame({"Date": ["2026-11-20"], "Code": ["B"]})
    combined = src._combined("bars", src._with_daily([old], [new]))
    assert list(combined.itertuples(index=False, name=None)) == [
        ("2026-11-19", "A"),
        ("2026-11-20", "B"),
    ]
