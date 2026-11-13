import json
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook

from trading_system.research import swing_earnings as e
from trading_system.research import swing_forward as sf
from trading_system.research.swing_earnings_review import EarningsReview


def test_xlsx_states():
    b = Workbook()
    sh = b.active
    sh.append(["見出し"])
    sh.append(["決算発表予定日\nScheduled", "コード\nCode"])
    sh.append([datetime(2026, 11, 18), "123A"])
    sh.append(["未定", 2345])
    f = BytesIO()
    b.save(f)
    assert e.parse_book(f.getvalue()) == [["123A0", "2026-11-18"], ["23450", None]]
    start, end = "2026-11-16", "2026-11-25"

    def state(d):
        return e.classify([[x, 0] for x in d], True, start, end)["state"]

    assert (
        state(["2026-11-18"]) == "during_hold"
        and state([start]) == state([end]) == "boundary_time_unknown"
    )
    assert state(["2026-11-18", end]) == "conflicting" and state([None]) == "date_unknown"
    assert state([]) == "not_listed" and state(["2026-12-01"]) == "outside_hold"
    assert e.classify([], False, start, end)["state"] == "unavailable"


def test_snapshot_cutoff_and_tamper(tmp_path, monkeypatch):
    now = datetime.now(e.UTC)
    folder = tmp_path / "earnings" / now.astimezone(e.JST).date().isoformat() / "123"
    folder.mkdir(parents=True)
    events = b'{"12340":[["2026-11-18",0]]}'
    for file, body in [
        ("page.html", b"page"),
        ("book-0.xlsx", b"workbook"),
        ("events.json", events),
    ]:
        (folder / file).write_bytes(body)
    meta = dict(
        page=e.PAGE,
        page_sha256=e.digest(b"page"),
        events_sha256=e.digest(events),
        sources=[dict(file="book-0.xlsx", sha256=e.digest(b"workbook"), url=e.PAGE)],
        observed_at=now.isoformat(),
        available_at=now.isoformat(),
    )
    (folder / "manifest.json").write_text(json.dumps(meta))
    assert e.asof(tmp_path, now - timedelta(seconds=1))[0]["state"] == "unavailable"
    assert e.asof(tmp_path, now)[1]["12340"][0][0] == "2026-11-18"
    (folder / "book-0.xlsx").write_bytes(b"changed")
    assert e.asof(tmp_path, now)[0]["state"] == "unavailable"


def test_reference_accounts_and_matched_profit_loss(tmp_path, monkeypatch):
    root = tmp_path / "data/swing-forward"
    sf.write(root / "initial.json", dict(start=sf.START, accounts=sf.initial()))
    days = [
        "2026-11-13",
        "2026-11-16",
        "2026-11-17",
        "2026-11-18",
        "2026-11-19",
        "2026-11-20",
        "2026-11-24",
        "2026-11-25",
        "2026-11-26",
        "2026-11-27",
        "2026-11-30",
        "2026-12-01",
        "2026-12-02",
        "2026-12-03",
    ]
    buyday, due = days[1], days[7]
    codes = ["12340", "23450", "34560"]

    def prices(jq, dates, now):
        on_exit = dates == [due]
        return (
            {
                c: [
                    {
                        "O": p if on_exit else 100,
                        "C": p if on_exit else 100,
                        "AdjFactor": 1,
                        "ExRT": None,
                    }
                ]
                for c, p in zip(codes, [80, 120, 100])
            },
            {},
            [],
        )

    monkeypatch.setattr(sf, "dated_inputs", prices)
    rows = [dict(code=c, estimated_debit="10015", plus3_exclusions=[]) for c in codes]
    audit = EarningsReview(sf.ACCOUNTS)
    for i, d in enumerate(days[1:8], 1):
        before = sf.get_books(root, days[i - 1])
        pre = dict(
            target=d,
            exit_if_bought=days[i + 6],
            accounts_before=before,
            stopped=False,
            signal=dict(candidates=rows if d == buyday else [], credit_complete=True),
            earnings=dict(
                annotations={
                    c: {"state": "during_hold" if c != codes[-1] else "boundary_time_unknown"}
                    for c in codes
                }
            ),
            machine={
                k: sf.account_orders(before[k], rows if d == buyday else [], d)
                for k in sf.ACCOUNTS[:2]
            },
        )
        folder = root / "sessions" / d
        sf.write(folder / "preopen.json", pre)
        sf.closing(tmp_path, root, d, days, datetime.now(e.UTC))
        close = sf.read(folder / "close.json")
        audit.update(close, pre, None)
        if d == buyday:
            assert len(close["accounts"]["machine_current"]["positions"]) == 3
            assert set(
                close["earnings_counterfactual"]["machine_current"]["account"]["positions"]
            ) == {codes[-1]}
            assert audit.report()["machine_current"]["matched_omission_net_yen"] is None
    r = audit.report()["machine_current"]
    assert r["closed"] == 2 and r["pending"] == 0 and Decimal(r["avoided_loss_yen"]) == 2029
    assert Decimal(r["missed_gain_yen"]) == 1969 and Decimal(r["portfolio_difference_yen"]) == 60
