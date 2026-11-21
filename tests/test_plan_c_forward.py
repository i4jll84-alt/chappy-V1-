from datetime import datetime

import pytest

from trading_system.plan_holdings import positions
from trading_system.research import c1_execution as engine
from trading_system.research import plan_c_forward as adapter

DAYS = ["2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27", "2026-11-30", "2026-12-01"]
CODE = "12340"


def instant(text):
    return datetime.fromisoformat(text).replace(tzinfo=adapter.JST)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    book = tmp_path / "data/plan-forward/accounts/C"
    monkeypatch.setattr(adapter, "calendar", lambda *a: (DAYS, []))

    def load(project, signal, cutoff, **settings):
        assert cutoff.tzinfo is not None and not settings["enable_vol_circuit_breaker"]
        return dict(
            ranked=[dict(code=CODE, sector="1000", close=500, atr14=20, deviation=-0.03)],
            marks={CODE: dict(close=500, sma5=510)},
            earnings={CODE: []},
            allow_entries=True,
            coverage_ok=True,
            reasons=[],
            counts={"data_ready": 2500},
            refs=[],
        )

    monkeypatch.setattr(adapter.c1_inputs, "load_day", load)

    def bars(project, day, cutoff):
        if cutoff < adapter.at(day, 15, 30):
            raise FileNotFoundError("official_daily_bars_not_available")
        return {CODE: dict(open=500, low=480, high=520, close=510, adj_factor=1)}

    monkeypatch.setattr(adapter.c1_inputs, "load_bars", bars)

    def advance(time):
        return adapter.advance(tmp_path, book, start=DAYS[0], capital=3000000, now=instant(time))

    return book, advance


def test_first_decision_only_after_start_and_account_is_independent(setup):
    book, run = setup
    before = run("2026-11-21T20:00")
    assert before["cash"] == before["nav"] == 3000000 and before["status"] == "waiting_signal"
    assert not (book.parent / "A").exists() and not (book.parent / "B").exists()
    assert not (book / "sessions").exists()
    ready = run("2026-11-24T17:00")
    assert ready["orders"] == 1 and ready["next_entry"] == DAYS[1]
    frozen = book / "sessions" / DAYS[1] / "decision.json"
    before_open = frozen.read_bytes()
    assert run("2026-11-25T08:59")["orders"] == 1
    assert not (frozen.parent / "open.json").exists()
    assert run("2026-11-25T09:01")["status"] == "waiting_close_inputs"
    closed = run("2026-11-25T18:00")
    assert closed["last_mark"] == DAYS[1]
    assert positions(book.parents[3], "C", closed)[0]["entry_price"] > 500
    close = frozen.with_name("close.json")
    original = close.read_bytes()
    assert run("2026-11-25T18:01") == closed
    assert before_open == frozen.read_bytes() and original == close.read_bytes()


def test_no_retroactive_purchases_after_open(setup):
    book, run = setup
    snapshot = run("2026-11-25T18:00")
    first = adapter.files.read(book / "sessions" / DAYS[1] / "decision.json")
    assert first["state"] == "missed_preopen" and first["plan"]["buys"] == []
    assert snapshot["last_mark"] == DAYS[1]
    assert snapshot["nav"] == 3000000 and not snapshot["positions"]


def test_computation_finishing_at_deadline_discards_new_entries(setup):
    book, _ = setup
    ticks = iter([instant("2026-11-25T08:59:59"), instant("2026-11-25T09:00:00")])
    decision = adapter.freeze(
        book.parents[3],
        book,
        engine.initial_account(),
        DAYS[0],
        DAYS[1],
        DAYS,
        [],
        lambda: next(ticks),
        adapter.read_settings(book.parents[3]),
    )
    assert decision["state"] == "missed_preopen"
    assert not decision["plan"]["buys"] and not decision["ranked"]


def test_preopen_stop_on_already_frozen_plan_preserves_snapshot(setup):
    book, run = setup
    run("2026-11-24T17:00")
    frozen = book / "sessions" / DAYS[1] / "decision.json"
    original = frozen.read_bytes()
    (book / "STOP").touch()
    run("2026-11-25T08:45")
    (book / "STOP").unlink()
    snapshot = run("2026-11-25T17:00")
    assert frozen.read_bytes() == original
    assert not snapshot["positions"] and snapshot["cash"] == 3000000


def test_settings_reject_invalid_switch(tmp_path):
    path = tmp_path / "config/c1-pullback.json"
    path.parent.mkdir()
    path.write_text('{"enable_vol_circuit_breaker":"false"}')
    with pytest.raises(ValueError, match="invalid_c1_settings"):
        adapter.read_settings(tmp_path)


def test_disk_staging_crossing_nine_discards_purchase(setup):
    book, _ = setup
    now = instant("2026-11-25T08:59:59")
    decision = adapter.freeze(
        book.parents[3],
        book,
        engine.initial_account(),
        DAYS[0],
        DAYS[1],
        DAYS,
        [],
        lambda: now,
        adapter.read_settings(book.parents[3]),
    )
    path = book / "sessions" / DAYS[1] / "decision.json"
    saved = adapter.publish_decision(path, decision, None, lambda: instant("2026-11-25T09:00:00"))
    assert saved["state"] == "missed_preopen" and not saved["plan"]["buys"]
    assert adapter.files.read(path) == saved
    assert not path.with_suffix(".pending").exists()


def test_unverified_stop_waits_then_resumes_from_frozen_open(setup, monkeypatch):
    book, run = setup
    run("2026-11-24T19:00")
    bars = {CODE: dict(open=500, high=530, low=None, close=525, adj_factor=1)}
    monkeypatch.setattr(adapter.c1_inputs, "load_bars", lambda *args: bars)
    pending = run("2026-11-25T17:00")
    folder = book / "sessions" / DAYS[1]
    assert pending["status"] == "waiting_close_inputs" and pending["last_mark"] is None
    opening = (folder / "open.json").read_bytes()
    assert not (folder / "close.json").exists()
    bars[CODE]["low"] = 450
    confirmed = run("2026-11-25T17:05")
    assert confirmed["last_mark"] == DAYS[1] and not confirmed["positions"]
    assert confirmed["nav"] < 3000000
    assert (folder / "open.json").read_bytes() == opening
    assert len(adapter.files.read(folder / "close.json")["account"]["trades"]) == 2


@pytest.mark.parametrize("recover", [True, False])
def test_missing_evening_inputs_can_recover_only_before_deadline(setup, monkeypatch, recover):
    book, run = setup
    valid = adapter.c1_inputs.load_day

    def incomplete(*args, **kwargs):
        return dict(
            valid(*args, **kwargs),
            coverage_ok=False,
            allow_entries=False,
            ranked=[],
            reasons=["data_ready_below_2500"],
        )

    monkeypatch.setattr(adapter.c1_inputs, "load_day", incomplete)
    pending = run("2026-11-24T20:00")
    assert pending["status"] == "waiting_inputs"
    folder = book / "sessions" / DAYS[1]
    assert list((folder / "attempts").glob("*.json")) and not (folder / "decision.json").is_file()
    monkeypatch.setattr(adapter.c1_inputs, "load_day", valid)
    if recover:
        assert run("2026-11-25T08:50")["orders"] == 1
    run("2026-11-25T17:00")
    decision = adapter.files.read(folder / "decision.json")
    assert len(decision["plan"]["buys"]) == int(recover)
    assert decision["state"] == ("ready" if recover else "insufficient_data")
