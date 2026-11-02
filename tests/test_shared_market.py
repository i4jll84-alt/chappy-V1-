import json
import sqlite3
from contextlib import closing
from datetime import timedelta

from test_research_panel import CODES, T, sample

from trading_system import kabu_study, research_panel
from trading_system.research.shared_market import latest_asof, publish


def test_shared_data_preserves_broker_times_sides_missing_and_revisions(tmp_path):
    root = tmp_path / "data"
    r = root / "rss-panel"
    k = root / "mufg-panel"
    r.mkdir(parents=True)
    k.mkdir()
    payload = sample()
    payload["items"][0]["bars"].append(["2026/11/04", "09:07", "", "", "", "", 0])
    with closing(sqlite3.connect(r / "research.sqlite")) as db:
        research_panel.initialize(db)
        research_panel.save(db, CODES, payload, T, T)
    with closing(sqlite3.connect(k / "research.sqlite")) as db:
        kabu_study.tables(db)
        record = dict(
            operation="board",
            code="7203",
            http_status=200,
            outcome="ok",
            reason=None,
            read_started_at=T.isoformat(),
            read_completed_at=(T + timedelta(seconds=1)).isoformat(),
        )
        body = json.dumps(
            {
                "Symbol": "7203",
                "Exchange": 1,
                "CurrentPriceTime": T.isoformat(),
                "CurrentPrice": 100,
                "Buy1": {"Price": 98, "Qty": 4},
                "Sell1": {"Price": 102, "Qty": 5},
                "VWAP": 99,
            }
        ).encode()
        kabu_study.store_sample(db, record, body)
    for _ in range(2):
        path = publish(tmp_path, T.date())
        with closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            assert len(latest_asof(db, T - timedelta(microseconds=1))) == 0
            assert {row["source"] for row in latest_asof(db, T)} == {"rakuten"}
            pair = {
                row["source"]: row
                for row in latest_asof(db, T + timedelta(seconds=1))
                if row["symbol"] == "7203"
            }
            assert (
                len(pair) == 2
                and pair["mufg_esmart"]["bid"] == 98
                and pair["rakuten"]["ask"] == 101
            )
            assert pair["mufg_esmart"]["day_volume"] is None and pair["mufg_esmart"]["vwap"] == 99
            assert pair["rakuten"]["jquants_code"] == pair["mufg_esmart"]["jquants_code"] == "72030"
            assert (
                db.execute(
                    "SELECT count(*) FROM minute_bars WHERE o IS NULL AND volume=0"
                ).fetchone()[0]
                == 1
            )
            assert db.execute("SELECT count(*) FROM quotes").fetchone()[0] == 32
