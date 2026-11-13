from datetime import date, datetime
from decimal import Decimal

import pytest

from trading_system.research import swing_macro as macro
from trading_system.research import swing_nikkei as nk


def test_same_contract_and_report_date():
    heading = "Nikkei 225 Futures\n2026年 11月12日(木曜日)\n夜間 Night Session"
    line = "202612 12.10 161120018 61,380 62,350 61,360 62,020 61,450 61,870 60,700 60,700"
    d = date(2026, 11, 12)
    info = nk.parse_night(heading + "\n" + line, d, d)
    assert (
        info["previous_value"] == "61380"
        and info["value"] == "62020"
        and info["contract"] == "202612"
    )
    for broken in [line.replace("61,380", "…"), line.replace("0018", "0019")]:
        with pytest.raises(ValueError):
            nk.parse_night(heading + "\n" + broken, d, d)
    assert nk.parse_night(heading + "\n" + line, d, date(2026, 12, 11))["state"] == "stale"
    with pytest.raises(ValueError):
        nk.parse_night(heading + "\n" + line, date(2026, 11, 13), d)


def test_clocks_units_and_review():
    now = datetime.fromisoformat("2026-11-13T07:55:00+09:00")
    body = b"observation_date,SP500\n2026-11-10,100\n2026-11-11,.\n2026-11-12,110\n2026-11-13,130"
    item = macro.latest("SP500", body, now)
    assert item["date"] == "2026-11-12" and Decimal(item["change"]) == 10
    y = "基準日,10年\nR8.11.11,-0.05\nR8.11.12,0.01\n".encode("cp932")
    assert Decimal(macro.latest("JP10Y", y, now)["change"]) == 6
    xml = (
        b'<x><d time="2026-11-11"><c currency="JPY" rate="180"/><c currency="USD" rate="1.2"/></d>'
        b'<d time="2026-11-12"><c currency="JPY" rate="182"/><c currency="USD" rate="1.3"/></d></x>'
    )
    assert Decimal(macro.latest("USDJPY", xml, now)["value"]) == 140
    context = {"manifest_sha256": "test", "series": {"SP500": item, "JP10Y": {"state": "stale"}}}
    answer = dict(
        manifest_sha256="test", reason="mixed", indicators=["SP500"], defer={"12340": "reason"}
    )
    assert macro.validate_assessment(context, answer, ["12340"]) is None
    for change in [
        {"manifest_sha256": "wrong"},
        {"defer": {"X": "r"}},
        {"indicators": []},
        {"indicators": ["JP10Y"]},
    ]:
        assert macro.validate_assessment(context, answer | change, ["12340"])


def test_freeze_cutoff_raw_hash_and_revision(tmp_path, monkeypatch):
    import hashlib
    import json

    now = datetime.fromisoformat("2026-11-13T07:55:00+09:00")
    folder = tmp_path / "macro" / "2026-11-13" / "a"
    folder.mkdir(parents=True)
    p = folder / "SP500.csv"
    raw = b"observation_date,SP500\n2026-11-10,100\n2026-11-11,101"
    p.write_bytes(raw)
    series = {k: {"state": "unavailable"} for k in macro.SERIES}
    series["SP500"] = dict(
        **macro.latest("SP500", raw, now),
        raw=str(p.relative_to(tmp_path)),
        sha256=hashlib.sha256(raw).hexdigest(),
        observed_at=now.isoformat(),
    )
    m = {"target": "2026-11-13", "available_at": now.isoformat(), "series": series}
    (folder / "manifest.json").write_text(json.dumps(m))
    assert not macro.asof(tmp_path, now.replace(minute=54))["series"]
    assert macro.asof(tmp_path, now)["series"] == series
    p.write_bytes(raw + b"x")
    assert not macro.asof(tmp_path, now)["series"]


def test_month_boundary_uses_both_published_files():
    now = datetime.fromisoformat("2026-11-03T07:55:00+09:00")
    old = "基準日,10年\nR8.10.29,1\nR8.10.30,1.01\n".encode("cp932")
    current = "基準日,10年\nR8.11.2,1.03\n".encode("cp932")
    item = macro.latest("JP10Y", old, now, [current])
    assert item["date"] == "2026-11-02" and item["previous_date"] == "2026-10-30"
    assert Decimal(item["change"]) == 2


def test_market_defer_only_changes_comparison_book(tmp_path, monkeypatch):
    from trading_system.research import swing_forward as sf

    day = sf.START
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
    folder = root / "sessions" / day
    sf.write(root / "initial.json", dict(start=day, accounts=sf.initial()))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 16, 8, tzinfo=sf.JST)

    monkeypatch.setattr(sf, "datetime", Clock)
    monkeypatch.setattr(
        macro,
        "asof",
        lambda *_: dict(manifest_sha256="frozen", series={"NK225F": {"state": "usable"}}),
    )
    code = "12340"
    candidate = dict(code=code, estimated_debit="10015", plus3_exclusions=[])
    monkeypatch.setattr(sf, "screen", lambda *_: dict(candidates=[candidate], credit_complete=True))
    assert sf.morning(tmp_path, root, day, sessions, Clock.now()) == "frozen"
    group = dict(
        buy=[code],
        sell=[],
        reason="fixed",
        candidate_notes={code: "eligible"},
        market_review=dict(
            manifest_sha256="frozen",
            indicators=["NK225F"],
            reason="hypothesis",
            defer={code: "observed risk"},
        ),
    )
    plus = dict(group, market_review=dict(group["market_review"], manifest_sha256="wrong"))
    sf.record_agent(
        root,
        day,
        dict(
            target=day,
            input_sha256=sf.read(folder / "review-input.json")["input_sha256"],
            groups=dict(current=group, plus3=plus),
        ),
    )
    quote = {"O": "100", "C": "120", "AdjFactor": "1", "ExRT": None}
    monkeypatch.setattr(sf, "dated_inputs", lambda *_: ({code: [quote]}, {}, []))
    assert sf.closing(tmp_path, root, day, sessions, Clock.now()) == "settled"
    result = sf.read(folder / "close.json")
    shadow = result["macro_counterfactual"]
    assert code in result["accounts"]["agent_current"]["positions"]
    assert not shadow["agent_current"]["account"]["positions"]
    assert Decimal(shadow["agent_current"]["net_difference_yen"]) == -1985
    assert shadow["agent_plus3"]["issue"] == "source_mismatch"
    assert Decimal(shadow["agent_plus3"]["net_difference_yen"]) == 0
