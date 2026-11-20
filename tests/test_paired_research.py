import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from trading_system import research_panel as panel
from trading_system.research import shared_market
from trading_system.research.daytrade_universe import paired_symbols, panel_location
from trading_system.research_workbook import chart_batches


def roster(root, day, overlap):
    fixed = [f"{n}.T" for n in range(2000, 2030)]
    daily = fixed[:overlap] + [f"{n}.T" for n in range(3000, 3030 - overlap)]
    union = paired_symbols(fixed, daily)
    folder = root / "daily" / str(day)
    folder.mkdir(parents=True)
    decision = day - timedelta(days=3)
    info = dict(
        source_date=str(decision),
        target_session=str(day),
        selected_at=f"{decision}T18:00:00+09:00",
        published_at=f"{decision}T18:01:00+09:00",
        capture_symbols=union,
        fixed_symbols=fixed,
        selected=[{"code": s[:4] + "0"} for s in daily],
    )
    (folder / "selection.json").write_text(json.dumps(info))
    (folder / "panel.json").write_text(json.dumps(dict(version=2, symbols=union, session=str(day))))
    assert panel_location(root, day) == folder
    return fixed, daily, union


def test_union_interleaves_duplicates_once_and_respects_limit(tmp_path):
    for n in (0, 1, 11, 29, 30):
        f, d, symbols = roster(tmp_path / str(n), date(2026, 11, 16), n)
        batches = chart_batches(symbols)
        assert len(symbols) == 61 - n and set(symbols) == set(f + d + ["7203.T"])
        assert max(map(len, batches)) <= 50
        assert [s for group in batches for s in group] == symbols
        assert panel.panel_symbols(dict(version=2, symbols=symbols)) == symbols


def test_both_batches_publish_observations_without_fixed_rule_trades(tmp_path):
    day = date(2026, 11, 16)
    root = tmp_path / "data/rss-panel"
    fixed, daily, union = roster(root, day, 1)
    groups = chart_batches(union)
    with sqlite3.connect(root / "research.sqlite") as db:
        panel.initialize(db)

        def record(clock, batch, stage):
            start = datetime.fromisoformat(f"{day}T{clock}+09:00")
            end = start + timedelta(milliseconds=250)
            items = []
            for code in union:
                quoted = code in (fixed[0], daily[-1])
                px = 110 if stage == "sell" else 100
                q = (
                    [str(day), px, clock, px - 1, px, 100, 100, clock, clock, 100]
                    if quoted
                    else [None] * 10
                )
                local = code in groups[batch]
                bars = []
                if local and stage == "signal":
                    for t in ("09:00", "09:55", "09:56", "09:57", "09:58", "09:59"):
                        v = 101 if quoted and t != "09:00" else 100
                        bars.append([str(day), t, v, v, v, v, 100])
                items.append(
                    dict(
                        symbol=code,
                        quote=q,
                        bars=bars,
                        chart_status="streaming" if local else "not_polled",
                    )
                )
            return panel.save(
                db, union, dict(state="read", items=items, chart_batch=batch), start, end
            )

        # No use of the still open 09:59 bar at the decision.
        record("09:59:10", 0, "signal")
        record("09:59:25", 1, "signal")
        record("10:00:10", 0, "buy")
        record("10:30:10", 1, "sell")
    shared_market.after_stop(tmp_path, day, "rakuten")
    output = tmp_path / "data/research/shared-market"
    with sqlite3.connect(output / f"{day}.sqlite") as db:
        assert db.execute("SELECT count(*) FROM quotes").fetchone()[0] == 4 * len(union)
        assert db.execute("SELECT count(distinct symbol) FROM minute_bars").fetchone()[0] == len(
            union
        )
        assert db.execute("SELECT count(*) FROM minute_bars WHERE elapsed_minute=0").fetchone()[
            0
        ] == len(union)
    assert json.loads((output / "rakuten-publish.json").read_bytes())["state"] == "published"
    assert not (tmp_path / "data/research/paired-comparison").exists()
    assert not (output / "comparison-status.json").exists()


@pytest.mark.parametrize("broker", ["rakuten", "mufg_esmart"])
def test_publication_retries_storage_without_starting_retired_comparison(
    tmp_path, monkeypatch, broker
):
    day = date(2020, 11, 17)
    roster(tmp_path / "data/rss-panel", day, 0)
    output = tmp_path / "data/research/shared-market"
    output.mkdir(parents=True)
    monkeypatch.setattr(shared_market.time, "sleep", lambda _: None)
    attempts = []

    def publish(project, session):
        attempts.append((project, session))
        if len(attempts) == 1:
            raise ValueError("broker_state_already_open")
        return output / f"{session}.sqlite"

    monkeypatch.setattr(shared_market, "publish", publish)
    shared_market.after_stop(tmp_path, day, broker)
    assert attempts == [(tmp_path, day), (tmp_path, day)]
    status = json.loads((output / f"{broker}-publish.json").read_bytes())
    assert status["state"] == "published" and status["date"] == str(day)
    assert not (output / "comparison-status.json").exists()
    assert not (tmp_path / "data/research/paired-comparison").exists()


def test_subscribing_is_not_a_bar_sample(tmp_path, monkeypatch):
    day = date(2026, 11, 16)
    root = tmp_path / "data/rss-panel"
    _, _, union = roster(root, day, 0)
    groups = chart_batches(union)
    book = root / "test.xlsx"
    monkeypatch.setattr(panel, "configuration", lambda _: (union, book))
    now = datetime.fromisoformat(f"{day}T15:59:00+09:00")
    calls = []

    def reader(book, symbols, rows, *, batch):
        assert symbols == union
        ready = bool(calls) and calls[-1] == batch
        calls.append(batch)
        return dict(
            state="read",
            items=[
                dict(
                    symbol=s,
                    quote=[None] * 10,
                    chart_status="streaming" if ready and s in groups[batch] else "subscribing",
                    bars=[[str(day), "15:00", 1, 1, 1, 1, 100]]
                    if ready and s in groups[batch]
                    else [],
                )
                for s in symbols
            ],
        )

    panel.run(
        root,
        int(now.timestamp() * 1e9),
        "none",
        one_pass=True,
        clock=lambda: now,
        opener=lambda _: None,
        reader=reader,
        sleep=lambda _: None,
    )
    assert calls == [0, 0, 1, 1]
    with sqlite3.connect(root / "research.sqlite") as db:
        assert db.execute("select count(*) from quotes").fetchone()[0] == 4 * 61
        assert db.execute("select count(distinct symbol) from bars").fetchone()[0] == 61
    status = json.loads((root / "status.json").read_bytes())
    assert status["target_symbols"] == status["current_day_chart_symbols"] == 61
