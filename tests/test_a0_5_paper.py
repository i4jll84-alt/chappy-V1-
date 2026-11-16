from datetime import datetime

import pandas as pd
import pytest

from trading_system.research import a0_5, swing_forward
from trading_system.research import a0_5_book as book
from trading_system.research.a0_5_rules.config import A0_5
from trading_system.research.a0_5_rules.live_portfolio import buying_power
from trading_system.research.a0_5_view import describe

CAL = pd.bdate_range("2026-11-02", "2026-11-30").difference(
    pd.to_datetime(["2026-11-03", "2026-11-23"])
)
FRI = "2026-11-20"
TUE = "2026-11-24"
NOW = datetime.fromisoformat("2026-11-19T17:00:00+09:00")


def order(code, side, day, sector="0050", shares=100):
    return dict(
        Code=code,
        side=side,
        order_date=day,
        S33=sector,
        shares=shares,
        reason="A0_5" if side == "BUY" else "SMA10_BREAK",
    )


def bar(price=100, factor=1, rights=""):
    return dict(O=price, AdjFactor=factor, ExRT=rights)


def test_gap_funds_holiday_settlement_split_and_restart(tmp_path):
    s = book.execute(book.initial(), FRI, [order("10000", "BUY", FRI)], {"10000": bar()}, CAL, [])
    assert s["cash"] == pytest.approx(5e6 - 100 * 100 * 1.001 * 1.0005)
    s = book.execute(s, TUE, [order("10000", "SELL", TUE)], {"10000": bar(55, 0.5, "1")}, CAL, [])
    assert s["journal"][-1]["shares"] == 200 and not s["positions"]
    assert s["settlements"][0]["date"] == "2026-11-26"
    assert buying_power(s, "2026-11-25") == pytest.approx(5e6 - 100 * 100 * 1.001 * 1.0005)
    assert buying_power(s, "2026-11-26") == s["cash"]
    assert book.audit(s)["compliance"] == 1 and book.audit(s)["win_rate"] == 1
    assert book.audit(s)["actual_one_way_bp"] is None
    book.save(tmp_path, s)
    restored = book.load(tmp_path)
    assert book.execute(restored, TUE, [], {}, CAL, []) == s
    assert book.audit(s)["realized"] == pytest.approx(s["cash"] - 5e6)


def test_holiday_t_plus_two_and_unfilled_buy_reasons():
    s = book.initial()
    s["cash"] = 50
    s["positions"] = [
        dict(Code="10000", shares=100, S33="0050", cost=10000, entry_date="2026-11-02")
    ]
    orders = [order("10000", "SELL", FRI), order("20000", "BUY", FRI), order("30000", "BUY", FRI)]
    after = book.execute(
        s, FRI, orders, {"10000": bar(), "20000": bar(), "30000": bar(50, 0.5, "1")}, CAL, []
    )
    assert after["settlements"][0]["date"] == "2026-11-25"
    assert buying_power(after, TUE) == 50
    assert [r["status"] for r in after["journal"]] == [
        "仮想約定",
        "受渡拘束控除後の寄付資金不足",
        "権利落ち・権利情報未確定",
    ]


def test_conditional_buy_requires_slot_and_failed_sale_carries():
    s = book.initial()
    s["positions"] = [
        dict(Code=c, S33="0050", entry_date="2026-11-05", shares=100, cost=10000)
        for c in ("10000", "20000")
    ]
    orders = [order("30000", "BUY", FRI), order("10000", "SELL", FRI)]
    r = book.execute(s, FRI, orders, {"10000": bar(None), "20000": bar(), "30000": bar()}, CAL, [])
    assert (
        r["journal"][0]["status"] == "始値欠損"
        and r["journal"][1]["status"] == "売り未成立・保有枠不足"
    )
    assert r["positions"][0]["pending_exit"] == "SMA10_BREAK"
    with pytest.raises(ValueError, match="権利"):
        book.execute(r, TUE, [], {"10000": bar(1, 0.3, "3"), "20000": bar()}, CAL, [])


def test_off_on_existing_four_books_and_no_broker_calls(tmp_path, monkeypatch):
    from trading_system import desktop_launcher
    from trading_system.data_sources.jquants import JQuantsClient

    def forbidden(*a, **kw):
        pytest.fail("broker or external data client invoked")

    monkeypatch.setattr(desktop_launcher, "launch_command", forbidden)
    monkeypatch.setattr(JQuantsClient, "__init__", forbidden)
    old = swing_forward.initial()
    assert len(old) == 4 and all(row["cash"] == "2000000" for row in old.values())
    path = tmp_path / "data/swing-forward/initial.json"
    swing_forward.write(path, old)
    frozen = path.read_bytes()
    assert a0_5.after_daily(tmp_path, "2026-11-19") is None
    assert not a0_5.root_for(tmp_path).exists()
    assert not a0_5.status(tmp_path, NOW)["enabled"]
    a0_5.select(tmp_path, True)

    def fake(project, day, now, s):
        return dict(
            state="ready",
            date=str(day.date()),
            order_date=FRI,
            cutoff=now.isoformat(),
            orders=[],
            candidates=[],
        )

    monkeypatch.setattr(a0_5, "assess", fake)
    first = a0_5.run(tmp_path, now=NOW)
    root = a0_5.root_for(tmp_path)
    before = book.load(root)
    assert a0_5.run(tmp_path, now=NOW) == first and before == book.load(root)
    assert before["cash"] == A0_5.capital
    assert len(list(root.rglob("plan.csv"))) == 1
    (path.parent / "STOP").touch()
    assert a0_5.run(tmp_path, now=NOW)["state"] == "stopped"
    assert a0_5.status(tmp_path, NOW)["decision"] is None
    (path.parent / "STOP").unlink()

    def fail(*args):
        raise ValueError("有効信用残不足")

    monkeypatch.setattr(a0_5, "assess", fail)
    result = a0_5.run(tmp_path, now=NOW, revise=True)
    assert (
        result["state"] == "blocked"
        and not (
            root / "sessions" / "2026-11-19" / f"v{result['saved_revision']:06d}" / "plan.csv"
        ).exists()
    )
    assert "予定生成停止" in describe(a0_5.status(tmp_path, NOW))
    a0_5.select(tmp_path, False)
    assert path.read_bytes() == frozen and swing_forward.initial() == old


def test_manual_calendar_cutoff_and_cross_day(tmp_path):
    now = NOW
    a0_5.manual_schedule(tmp_path, "10000", "none", "会社IR 11月19日", now)
    manual = book.load(a0_5.root_for(tmp_path))["manual"]
    info, _ = a0_5.inputs.schedules(tmp_path, ["10000"], pd.Timestamp("2026-11-19"), now, manual)
    assert info["10000"][0] == ""
    earlier = now.replace(hour=16)
    assert (
        a0_5.inputs.schedules(tmp_path, ["10000"], pd.Timestamp("2026-11-19"), earlier, manual)[0][
            "10000"
        ][0]
        is None
    )
    later = now.replace(day=20)
    assert (
        a0_5.inputs.schedules(tmp_path, ["10000"], pd.Timestamp(FRI), later, manual)[0]["10000"][0]
        is None
    )


def test_stop_seen_at_open_survives_removal_and_prior_date_cannot_rescreen(tmp_path):
    from trading_system.research.a0_5 import assess, opening

    root = a0_5.root_for(tmp_path)
    a0_5.select(tmp_path, True)
    (root / "STOP").touch()
    assert opening(tmp_path, NOW)["state"] == "stopped"
    (root / "STOP").unlink()
    assert (root / "stops/2026-11-19.json").is_file()
    s = book.execute(
        book.initial(),
        FRI,
        [dict(order("10000", "BUY", FRI), stop_observed=True)],
        {"10000": bar()},
        CAL,
        [],
    )
    assert s["cash"] == A0_5.capital and not s["positions"] and not book.audit(s)["fills"]
    with pytest.raises(ValueError, match="過去日"):
        assess(tmp_path, pd.Timestamp("2026-11-18"), NOW, s)


def test_bulk_cutoff_and_revision_hash(tmp_path):
    import sqlite3

    from trading_system.research.a0_5_inputs import bulk, sha

    with sqlite3.connect(tmp_path / "history.sqlite") as db:
        db.execute(
            "create table bulk_files(object_key,modified,raw_path,sha256,parquet_path,"
            "parquet_sha256,observed_at,available_at,endpoint,state,min_date,max_date)"
        )
        for i, stamp in enumerate(("2026-11-19T15:00:00+09:00", "2026-11-19T19:00:00+09:00")):
            raw = tmp_path / f"raw{i}.json"
            raw.write_text(str(i))
            part = tmp_path / f"part{i}.parquet"
            pd.DataFrame([dict(Date="2026-11-19", C=i + 1)]).to_parquet(part)
            db.execute(
                "insert into bulk_files values(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "same-key",
                    stamp,
                    raw.name,
                    sha(raw),
                    part.name,
                    sha(part),
                    stamp,
                    stamp,
                    "/topix",
                    "complete",
                    "2026-11-19",
                    "2026-11-19",
                ),
            )

    def read(at):
        refs = []
        frames = bulk(tmp_path, "/topix", ["Date", "C"], "2026-11-19", "2026-11-19", at, refs)
        return frames[0].C.item(), refs

    assert read(NOW)[0] == 1 and read(NOW.replace(hour=20))[0] == 2
    (tmp_path / "raw1.json").write_text("changed")
    assert read(NOW)[0] == 1
    with pytest.raises(ValueError, match="検証失敗"):
        read(NOW.replace(hour=20))


def test_frozen_exports_preserve_bytes_and_recover_missing_csv(tmp_path, monkeypatch):
    import json

    root = tmp_path / "paper"
    record = dict(state="ready", date="2026-11-16", orders=[], candidates=[])
    a0_5.export(root, record, 2)
    folder = root / "sessions/2026-11-16/v000002"
    decision, plan = folder / "decision.json", folder / "plan.csv"
    # Existing releases may have used a different field order.
    original = json.dumps(record, indent=4).encode()
    decision.write_bytes(original)
    expected_csv = plan.read_bytes()
    plan.unlink()
    a0_5.export(root, record, 2)
    assert decision.read_bytes() == original and plan.read_bytes() == expected_csv
    monkeypatch.setattr(a0_5, "atomic_write", lambda *args: pytest.fail("frozen file rewritten"))
    a0_5.export(root, dict(reversed(list(record.items()))), 2)
    plan.write_bytes(b"changed")
    with pytest.raises(ValueError, match="保存済み予定が不一致"):
        a0_5.export(root, record, 2)
    assert plan.read_bytes() == b"changed"
    decision.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="保存済み判定が不一致"):
        a0_5.export(root, record, 2)
    assert decision.read_bytes() == b"{}"
