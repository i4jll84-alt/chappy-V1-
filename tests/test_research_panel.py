import gzip
import json
import sqlite3
from contextlib import closing
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
                "chart_status": "streaming",
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


def test_chart_dates_use_valid_rows_and_tokyo_observation_date():
    payload = sample()
    next_day = T.replace(day=5, hour=0, minute=5).astimezone(p.UTC)
    with sqlite3.connect(":memory:") as db:
        p.initialize(db)

        def record():
            return p.save(db, CODES, payload, next_day, next_day)

        old = record()
        assert old["state"] == "read" and old["chart_symbols"] == 31
        assert old["current_day_chart_symbols"] == 0
        assert old["latest_chart_at"] == T.isoformat()
        today = ["2026/11/05", "00:02", "", "", "", "", 0]
        payload["items"][0]["bars"].append(today)
        payload["items"][1]["bars"].extend([today, [*today[:2], 10, 10, 10, 10, 100]])
        payload["items"][2]["bars"].append(["2026/11/05", "00:06", 10, 10, 10, 10, 100])
        mixed = record()
        assert mixed["current_day_chart_symbols"] == 1 and mixed["invalid_bars"] == 2
        assert mixed["latest_chart_at"] == "2026-11-05T00:02:00+09:00"
        for item in payload["items"]:
            item["bars"] = []
        empty = record()
        assert empty["current_day_chart_symbols"] == 0 and empty["latest_chart_at"] is None


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


@pytest.mark.parametrize(
    ("feed", "bars_day", "expected", "calls"),
    [
        ("connection_required", "2026/11/04", "incomplete_panel", 4),
        (None, "2026/11/04", "incomplete_panel", 4),
        ("streaming", "2026/11/03", "one_pass_recent_only", 4),
        ("recovered", "2026/11/04", "one_pass_complete", 2),
    ],
)
def test_snapshot_checks_feed_and_date(tmp_path, monkeypatch, feed, bars_day, expected, calls):
    now = T.replace(hour=20)
    monkeypatch.setattr(p, "configuration", lambda _: (CODES, tmp_path / "test.xlsx"))
    frames = []

    def read(*_):
        raw = sample()
        for item in raw["items"]:
            if feed is None:
                del item["chart_status"]
            elif feed == "recovered":
                item["chart_status"] = "streaming" if frames else "connection_required"
            else:
                item["chart_status"] = feed
            for bar in item["bars"]:
                bar[0] = bars_day
        frames.append(raw)
        return raw

    p.run(
        tmp_path,
        int(now.timestamp() * 1e9),
        "none",
        one_pass=True,
        clock=lambda: now,
        opener=lambda _: None,
        reader=read,
        sleep=lambda _: None,
    )
    status = json.loads((tmp_path / "status.json").read_bytes())
    assert status["reason"] == expected and len(frames) == calls
    assert status["connection_required_charts"] == (31 if feed == "connection_required" else 0)
    with sqlite3.connect(tmp_path / "research.sqlite") as db:
        saved = [
            json.loads(gzip.decompress(r[0]))
            for r in db.execute("select raw_gzip from frames order by id")
        ]
        assert [fr["items"] for fr in saved] == [fr["items"] for fr in frames]


def test_daytime_refreshes_full_history_once_after_reconnection(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "configuration", lambda _: (CODES, tmp_path / "test.xlsx"))
    sizes = []

    def read(_book, _codes, rows):
        sizes.append(rows)
        data = sample()
        if len(sizes) <= 2:
            data["items"][0]["chart_status"] = "connection_required"
        if len(sizes) == 5:
            publish_stop(tmp_path)
        return data

    p.run(
        tmp_path,
        int(T.timestamp() * 1e9),
        "none",
        clock=lambda: T,
        opener=lambda _: None,
        reader=read,
        sleep=lambda _: None,
    )
    assert sizes == [420, 12, 12, 420, 12]


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


def test_optional_index_failure_retains_stock_data_and_clears_status_on_recovery(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(p, "configuration", lambda _: (CODES, tmp_path / "panel.xlsx"))
    now = T
    states = []
    calls = 0

    def read(_book, _symbols, rows):
        nonlocal calls
        if calls:
            states.append(json.loads((tmp_path / "status.json").read_bytes()))
        payload = sample()
        if calls == 0:
            payload["indexes"] = []
            payload["index_issue"] = {
                "reason": "excel_call_rejected",
                "stage": "indexes",
                "hresult": "0x80010001",
            }
        else:
            payload["indexes"] = [{"code": "TOPX", "fields": ["2026/11/04", "09:10:00", 2500]}]
            publish_stop(tmp_path)
        calls += 1
        return payload

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
    assert states[0]["state"] == "read"
    assert states[0]["index_issue"] == "excel_call_rejected"
    assert "index_issue" not in json.loads((tmp_path / "status.json").read_bytes())
    with sqlite3.connect(tmp_path / "research.sqlite") as db:
        assert db.execute("select count(*) from quotes").fetchone()[0] == 62
        assert db.execute("select count(*) from bars").fetchone()[0] == 62
        assert db.execute("select count(*) from market_indexes").fetchone()[0] == 1
        rows = db.execute("select state,raw_gzip from frames order by id").fetchall()
        assert [row[0] for row in rows] == ["read", "read"]
        first = json.loads(gzip.decompress(rows[0][1]))
        assert first["index_issue"]["stage"] == "indexes"
        assert first["items"] == sample()["items"]


def test_saved_frame_reader_does_not_block_live_frame_commit(tmp_path):
    path = tmp_path / "research.sqlite"
    with closing(sqlite3.connect(path, timeout=0.05)) as writer:
        p.initialize(writer)
        payload = sample()
        assert p.save(writer, CODES, payload, T, T)["bar_versions"] == 62
        with closing(sqlite3.connect(path, timeout=0.05)) as reader:
            reader.execute("BEGIN")
            assert reader.execute("SELECT count(*) FROM frames").fetchone() == (1,)
            payload["items"][0]["bars"][0][5] = 102
            assert p.save(writer, CODES, payload, T, T)["bar_versions"] == 1
            assert writer.execute("SELECT count(*) FROM frames").fetchone() == (2,)
            assert reader.execute("SELECT count(*) FROM frames").fetchone() == (1,)
            reader.rollback()
            assert reader.execute("SELECT count(*) FROM frames").fetchone() == (2,)
        rows = writer.execute("SELECT raw_gzip FROM frames ORDER BY id").fetchall()
        closes = [json.loads(gzip.decompress(r[0]))["items"][0]["bars"][0][5] for r in rows]
        assert closes == [101, 102]


def test_database_busy_reason_is_distinct_from_other_storage_errors():
    busy = sqlite3.OperationalError("private details")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    assert p.failure_reason(busy) == "capture_database_busy"
    assert p.failure_reason(sqlite3.IntegrityError("private details")) == "research_storage_error"


def test_completed_trade_becoming_empty_is_flagged_without_replacing_raw():
    from trading_system.research.bar_quality import EMPTY_REGRESSION

    with sqlite3.connect(":memory:") as db:
        p.initialize(db)
        payload = sample()
        p.save(db, CODES, payload, T, T)
        before = db.execute("SELECT raw_gzip FROM frames WHERE id=1").fetchone()[0]
        item = payload["items"][0]
        item["bars"][0][2:] = ["", "", "", "", 0]
        # A minute without any previous completed trade remains a legitimate empty observation.
        item["bars"].append(["2026/11/04", "09:08", "", "", "", "", 0])
        result = p.save(db, CODES, payload, T, T + timedelta(seconds=1))
        assert result["suspect_empty_minutes"] == result["unresolved_empty_minutes"] == 1
        assert result["unresolved_today_empty_minutes"] == 1
        assert db.execute("SELECT * FROM bar_quality_events").fetchall() == [
            (CODES[0], "2026-11-04T09:09:00+09:00", 2, 1, 2, EMPTY_REGRESSION)
        ]
        assert db.execute(
            "SELECT frame,o,volume FROM bars WHERE symbol=? AND bar_time LIKE '%09:09:%' "
            "ORDER BY frame",
            (CODES[0],),
        ).fetchall() == [(1, 100, 500), (2, None, 0)]
        assert db.execute("SELECT raw_gzip FROM frames WHERE id=1").fetchone()[0] == before
        repeated = p.save(db, CODES, payload, T, T + timedelta(seconds=2))
        assert repeated["bar_versions"] == 0 and repeated["suspect_empty_minutes"] == 1
        assert db.execute("SELECT count(*) FROM bar_quality_events").fetchone()[0] == 1
        # A subsequent positive correction resolves the warning without erasing any version.
        item["bars"][0][2:] = [100, 103, 99, 102, 550]
        fixed = p.save(db, CODES, payload, T, T + timedelta(seconds=3))
        assert fixed["unresolved_empty_minutes"] == 0
        assert db.execute("SELECT count(*) FROM bar_quality_events").fetchone()[0] == 1


def test_new_quality_detector_preserves_old_versions_and_records_actual_detection_time():
    with sqlite3.connect(":memory:") as db:
        p.initialize(db)
        payload = sample()
        p.save(db, CODES, payload, T, T, annotate_quality=False)
        payload["items"][0]["bars"][0][2:] = ["", "", "", "", 0]
        p.save(db, CODES, payload, T, T + timedelta(seconds=1), annotate_quality=False)
        before = db.execute("SELECT * FROM bars ORDER BY frame,symbol,bar_time").fetchall()
        later = T + timedelta(days=1)
        result = p.save(db, CODES, payload, later, later)
        assert result["unresolved_empty_minutes"] == 1
        assert result["unresolved_today_empty_minutes"] == 0
        assert db.execute(
            "SELECT empty_frame,priced_frame,detected_frame FROM bar_quality_events"
        ).fetchone() == (2, 1, 3)
        # Only elapsed flags for yesterday's unfinished minute may change on the next day.
        assert (
            db.execute(
                "SELECT * FROM bars WHERE frame<=2 ORDER BY frame,symbol,bar_time"
            ).fetchall()
            == before
        )


def test_incomplete_bar_then_empty_is_not_claimed_as_a_completed_trade_regression():
    with sqlite3.connect(":memory:") as db:
        p.initialize(db)
        payload = sample()
        p.save(db, CODES, payload, T, T)
        payload["items"][0]["bars"][1][2:] = ["", "", "", "", 0]
        result = p.save(db, CODES, payload, T, T + timedelta(minutes=1))
        assert result["unresolved_empty_minutes"] == 0
