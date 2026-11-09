import gzip
import json
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from trading_system import research_panel as p
from trading_system.brokers.locking import single_writer
from trading_system.rss_stop import publish_stop, stop_ticket

T = datetime(2026, 11, 4, 9, 10, tzinfo=p.JST)
CODES = ["7203.T"] + [f"{n}.T" for n in range(1000, 1030)]


def sample():
    return {
        "state": "read",
        "items": [
            {
                "symbol": c,
                "quote": [
                    "2026/11/04",
                    100,
                    "09:09:59",
                    99,
                    101,
                    200,
                    100,
                    "09:09:59",
                    "09:10:00",
                    100,
                ],
                "bars": [
                    ["2026/11/04", "09:09", 100, 102, 99, 101, 500],
                    ["2026/11/04", "09:10", 101, 102, 100, 101, 200],
                ],
            }
            for c in CODES
        ],
    }


def test_frame_revisions_and_bid_ask_are_known_only_when_read():
    with sqlite3.connect(":memory:") as db:
        p.initialize(db)
        payload = sample()

        def save(at):
            return p.save(db, CODES, payload, at, at)

        assert save(T)["bar_versions"] == 62
        assert save(T)["bar_versions"] == 0
        assert db.execute("select sum(elapsed_minute) from bars").fetchone()[0] == 31
        payload["items"][0]["bars"][0][5] = 102
        assert save(T + timedelta(minutes=1))["bar_versions"] == 32
        assert (
            db.execute("select count(*) from quotes where quality='observed_book'").fetchone()[0]
            == 62
        )
        assert (
            json.loads(
                gzip.decompress(
                    db.execute("select raw_gzip from frames order by id limit 1").fetchone()[0]
                )
            )["items"][0]["bars"][0][5]
            == 101
        )
        payload["items"].reverse()
        assert save(T)["state"] == "panel_mismatch"
        assert db.execute("select count(*) from quotes").fetchone()[0] == 93


def test_bad_and_conflicting_bars_are_raw_only():
    payload = sample()
    item = payload["items"][0]
    item["bars"] += [
        ["2026/11/04", "09:09", 100, 105, 99, 101, 500],
        ["2026/11/04", "09:11", 100, 102, 99, 101, 500],
        ["2026/11/04", "09:08", 100, 102, 120, 101, 500],
    ]
    item["quote"][3] = 103
    with sqlite3.connect(":memory:") as db:
        p.initialize(db)
        v = p.save(db, CODES, payload, T, T)
        assert v["invalid_bars"] == 3 and v["books"] == 30
        assert (
            db.execute("select count(*) from bars where symbol=?", (CODES[0],)).fetchone()[0] == 1
        )
        assert (
            p.save(db, CODES, payload, T, T - timedelta(seconds=1))["state"] == "clock_regression"
        )


@pytest.mark.parametrize("manual", [True, False])
def test_button_stop_or_deadline_then_explicit_restart(tmp_path, monkeypatch, manual):
    monkeypatch.setattr(p, "configuration", lambda _: (CODES, tmp_path / "dummy.xlsx"))
    clock = [T.replace(hour=15, minute=58, second=50)]
    calls = []

    def read(*_):
        calls.append(1)
        if manual:
            publish_stop(tmp_path)
        else:
            clock[0] = clock[0].replace(hour=16, minute=0)
        return {"state": "unavailable", "reason": "fixture"}

    def run(ticket="none"):
        p.run(
            tmp_path,
            int(clock[0].timestamp() * 1e9),
            ticket,
            clock=lambda: clock[0],
            opener=lambda _: None,
            reader=read,
            sleep=lambda _: None,
        )

    run()
    assert json.loads((tmp_path / "status.json").read_bytes())["reason"] == (
        "manual_stop" if manual else "daytime_1600"
    )
    clock[0] = T + timedelta(days=1)
    if manual:
        run()
        assert len(calls) == 1
    marker = tmp_path / "STOP"
    run(stop_ticket(marker) if marker.exists() else "none")
    assert len(calls) == 2


@pytest.mark.parametrize("hour,minute", [(5, 59), (15, 59), (16, 0)])
def test_no_startfile_outside_daytime_or_duplicate(tmp_path, hour, minute):
    now = T.replace(hour=hour, minute=minute)
    opener = Mock()
    p.run(tmp_path, int(now.timestamp() * 1e9), "none", clock=lambda: now, opener=opener)
    opener.assert_not_called()
    with single_writer(tmp_path), pytest.raises(ValueError, match="already_open"):
        p.run(tmp_path, int(T.timestamp() * 1e9), "none", clock=lambda: T, opener=opener)
    opener.assert_not_called()


def test_evening_snapshot_opens_once_and_keeps_empty_minutes_and_indexes(tmp_path, monkeypatch):
    now = T.replace(hour=20)
    monkeypatch.setattr(p, "configuration", lambda _: (CODES, tmp_path / "fixture.xlsx"))
    raw = sample()
    raw["items"][0]["bars"].append(["2026/11/04", "09:08", "", "", "", "", 0])
    raw["indexes"] = [{"code": "N225", "fields": ["2026/11/04", "15:45", 45000]}]
    opener = Mock()
    reader = Mock(return_value=raw)
    p.run(
        tmp_path,
        int(now.timestamp() * 1e9),
        "none",
        one_pass=True,
        clock=lambda: now,
        opener=opener,
        reader=reader,
        sleep=lambda _: None,
    )
    opener.assert_called_once()
    reader.assert_called_once()
    with sqlite3.connect(tmp_path / "research.sqlite") as db:
        assert db.execute("select count(*) from bars where o is null and volume=0").fetchone() == (
            1,
        )
        assert db.execute("select code,quality from market_indexes").fetchone() == (
            "N225",
            "observed",
        )
    assert json.loads((tmp_path / "status.json").read_bytes())["reason"] == "one_pass_complete"


def test_start_failure_records_specific_reason_without_opening_excel(tmp_path, monkeypatch):
    monkeypatch.setattr(
        p, "configuration", Mock(side_effect=ValueError("research_workbook_formulas_changed"))
    )
    opener = Mock()
    with pytest.raises(ValueError, match="research_workbook_formulas_changed"):
        p.run(tmp_path, int(T.timestamp() * 1e9), "none", clock=lambda: T, opener=opener)
    opener.assert_not_called()
    assert (
        json.loads((tmp_path / "status.json").read_bytes())["reason"]
        == "research_workbook_formulas_changed"
    )


def test_unknown_exception_text_is_not_written_to_status(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "configuration", Mock(side_effect=ValueError("private-detail")))
    with pytest.raises(ValueError):
        p.run(tmp_path, int(T.timestamp() * 1e9), "none", clock=lambda: T)
    status = (tmp_path / "status.json").read_text()
    assert "private-detail" not in status
    assert json.loads(status)["reason"] == "research_start_failed"


def test_full_read_recovery_and_bounded_retry_keep_all_observations(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "configuration", lambda _: (CODES, tmp_path / "panel.xlsx"))
    now = T
    calls = []
    outcomes = {
        0: "busy",
        1: "excel_call_rejected",
        2: "excel_retry_later",
        5: "busy",
        7: "excel_property_unavailable",
        9: "excel_range_shape",
    }

    def read(_book, _symbols, rows):
        attempt = len(calls)
        calls.append((now, rows))
        if attempt == 19:
            publish_stop(tmp_path)
        if attempt in outcomes:
            return {"state": "unavailable", "reason": outcomes[attempt], "stage": "ready"}
        return sample()

    def advance(seconds):
        nonlocal now
        now += timedelta(seconds=seconds)

    p.run(
        tmp_path,
        int(T.timestamp() * 1e9),
        "none",
        clock=lambda: now,
        sleep=advance,
        opener=lambda _: None,
        reader=read,
    )
    assert [i for i, (_, rows) in enumerate(calls) if rows == 420] == [0, 2, 4, 19]
    gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(calls, calls[1:])]
    assert gaps[:11] == [3, 15, 15, 15, 15, 3, 15, 3, 15, 3, 15]
    with sqlite3.connect(tmp_path / "research.sqlite") as db:
        raw = [
            json.loads(gzip.decompress(b))
            for (b,) in db.execute("SELECT raw_gzip FROM frames ORDER BY id")
        ]
    assert [r["requested_rows"] for r in raw] == [width for _, width in calls]
    assert {i: r["reason"] for i, r in enumerate(raw) if r["state"] == "unavailable"} == outcomes


@pytest.mark.parametrize("stop_method", ["marker", "deadline"])
def test_fast_retry_remains_interruptible(tmp_path, monkeypatch, stop_method):
    monkeypatch.setattr(p, "configuration", lambda _: (CODES, tmp_path / "panel.xlsx"))
    now = T
    reader = Mock(return_value={"state": "unavailable", "reason": "busy"})

    def interrupt(seconds):
        nonlocal now
        assert seconds == 0.5
        if stop_method == "marker":
            publish_stop(tmp_path)
        else:
            now = now.replace(hour=16)

    p.run(
        tmp_path,
        int(T.timestamp() * 1e9),
        "none",
        clock=lambda: now,
        sleep=interrupt,
        opener=lambda _: None,
        reader=reader,
    )
    reader.assert_called_once()
    status = json.loads((tmp_path / "status.json").read_bytes())
    assert status["reason"] == ("manual_stop" if stop_method == "marker" else "daytime_1600")


@pytest.mark.parametrize(
    "reply, reason",
    [
        (p.subprocess.TimeoutExpired("fixture", 45), "reader_timeout"),
        (OSError("sensitive"), "reader_launch_failed"),
        (p.subprocess.CompletedProcess([], 1, b"sensitive"), "reader_exit_failed"),
        (p.subprocess.CompletedProcess([], 0, b"bad-json"), "reader_invalid_output"),
    ],
)
def test_capture_process_failure_reasons(monkeypatch, tmp_path, reply, reason):
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setattr(p.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    runner = Mock(side_effect=reply) if isinstance(reply, Exception) else Mock(return_value=reply)
    monkeypatch.setattr(p.subprocess, "run", runner)
    assert p.capture(tmp_path / "book.xlsx", CODES, 12) == {
        "state": "unavailable",
        "reason": reason,
        "stage": "process",
    }
