from copy import deepcopy
from datetime import datetime

import pytest

from trading_system.research import plan_c_pead_forward as adapter

DAYS = ["2026-11-25", "2026-11-26", "2026-11-27", "2026-11-30", "2026-12-01", "2026-12-02"]
CODE = "12340"


def instant(text):
    return datetime.fromisoformat(text).replace(tzinfo=adapter.JST)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    book = tmp_path / "accounts/C"
    monkeypatch.setattr(adapter, "calendar", lambda *a: DAYS)
    data = dict(
        date=DAYS[0],
        topix_close=101,
        topix_sma20=100,
        candidates=[CODE],
        bars={
            CODE: dict(
                open=1000,
                high=1020,
                low=980,
                close=1000,
                sma20=950,
                atr14=20,
                avg_turnover20=1_000_000_000,
                factor=1,
                name="fixture",
                sector33="1000",
            )
        },
        alerts=[],
        audit=dict(financial_complete=True),
    )
    execution = deepcopy(data)
    execution.update(date=DAYS[1], candidates=[])

    def load(project, day, now, *, execution=False):
        if execution:
            return deepcopy(load.execution), DAYS
        row = deepcopy(data)
        row["date"] = day
        return row, DAYS

    load.execution = execution
    monkeypatch.setattr(adapter, "load_snapshot", load)

    def run(text):
        return adapter.advance(tmp_path, book, start=DAYS[0], capital=3_000_000, now=instant(text))

    return book, run, data, load


def test_prospective_independent_native_account_and_costs(setup):
    book, run, _, _ = setup
    early = run("2026-11-25T14:00")
    assert early["status"] == "waiting_signal" and early["nav"] == 3_000_000
    ready = run("2026-11-25T18:00")
    assert ready["orders"] == 1 and ready["coverage_ok"] and ready["allow_entries"]
    path = book / "sessions" / DAYS[1] / "decision.json"
    original = path.read_bytes()
    assert run("2026-11-26T08:59")["status"] == "waiting_for_open"
    result = run("2026-11-26T18:00")
    assert result["last_mark"] == DAYS[1]
    position = result["positions"][CODE]
    assert position["entry_price"] == pytest.approx(1001)
    assert position["cost"] == pytest.approx(1001 * position["shares"] * 1.0005)
    assert result["nav"] < 3_000_000
    assert original == path.read_bytes()
    close = path.with_name("close.json").read_bytes()
    run("2026-11-26T18:01")
    assert close == path.with_name("close.json").read_bytes()
    assert not (book.parent / "A").exists()
    assert not list(book.glob(".pead-*"))


@pytest.mark.parametrize("opening,expected", [(1050, 1050), (1051, None)])
def test_buy_never_exceeds_auction_limit(setup, opening, expected):
    book, run, _, loader = setup
    run("2026-11-25T18:00")
    row = loader.execution["bars"][CODE]
    row.update(open=opening, high=1070, low=1040, close=1055)
    result = run("2026-11-26T18:00")
    if expected is None:
        assert result["positions"] == {} and result["cash"] == 3_000_000
        close = adapter.files.read(book / "sessions" / DAYS[1] / "close.json")
        assert close["snapshot"]["bars"][CODE]["open"] == opening
    else:
        assert result["positions"][CODE]["entry_price"] == expected


def test_entry_day_stop_and_t_plus_two_cash(setup):
    book, run, _, loader = setup
    run("2026-11-25T18:00")
    loader.execution["bars"][CODE]["low"] = 900
    result = run("2026-11-26T18:00")
    assert result["positions"] == {}
    close = adapter.files.read(book / "sessions" / DAYS[1] / "close.json")
    assert close["snapshot"]["bars"][CODE]["low"] == 900
    native = close["operator_state"]
    fills = [f for rows in native["fills"].values() for f in rows]
    buy, sell = fills
    assert buy["side"] == "BUY" and sell["reason"] == "PAPER_HARD_STOP"
    assert sell["price"] == pytest.approx(921 * 0.999)
    assert sell["settlement_date"] == DAYS[3]
    assert result["cash"] > result["available_cash"]
    assert result["fees"] == pytest.approx(sum(f["fee"] for f in fills))


def test_late_start_does_not_reconstruct_purchases(setup):
    book, run, _, _ = setup
    result = run("2026-11-26T18:00")
    decision = adapter.files.read(book / "sessions" / DAYS[1] / "decision.json")
    assert decision["state"] == "missed_preopen" and not decision["picks"]
    assert not result["positions"] and result["nav"] == 3_000_000


def test_missing_execution_bar_retries_without_partial_state(setup):
    book, run, _, loader = setup
    run("2026-11-25T18:00")
    row = loader.execution["bars"][CODE]
    row["low"] = None
    waiting = run("2026-11-26T18:00")
    assert waiting["status"] == "waiting_close_inputs"
    assert not (book / "sessions" / DAYS[1] / "close.json").exists()
    row["low"] = 980
    result = run("2026-11-26T18:01")
    assert result["positions"][CODE]["shares"] > 0


def test_preopen_stop_is_persistent_without_changing_frozen_decision(setup):
    book, run, _, _ = setup
    run("2026-11-25T18:00")
    path = book / "sessions" / DAYS[1] / "decision.json"
    original = path.read_bytes()
    (book / "STOP").touch()
    run("2026-11-26T08:00")
    (book / "STOP").unlink()
    result = run("2026-11-26T18:00")
    assert result["positions"] == {} and result["cash"] == 3_000_000
    assert path.read_bytes() == original


def test_unverified_financial_bullish_waits_bearish_seals_no_buy(setup):
    book, run, source, _ = setup
    source["audit"]["financial_complete"] = False
    assert run("2026-11-25T18:00")["status"] == "waiting_inputs"
    assert not (book / "sessions" / DAYS[1] / "decision.json").exists()
    source["topix_close"] = 99
    ready = run("2026-11-25T19:00")
    assert ready["orders"] == 0 and not ready["allow_entries"]


def test_publication_at_deadline_discards_all_buys(setup):
    book, run, _, _ = setup
    run("2026-11-25T18:00")
    path = book / "sessions" / DAYS[1] / "decision.json"
    decision = adapter.files.read(path)
    path.unlink()
    sealed = adapter.publish_decision(path, decision, lambda: instant("2026-11-26T09:00"))
    assert sealed["state"] == "missed_preopen"
    assert (
        sealed["picks"] == sealed["plan"]["buys"] == sealed["operator_state"]["pending_buys"] == []
    )


def test_close_publication_crash_replay_is_idempotent(setup, monkeypatch):
    book, run, _, _ = setup
    run("2026-11-25T18:00")
    write = adapter.files.write

    def crash(path, value):
        if path.name == "close.json":
            raise OSError("simulated crash")
        return write(path, value)

    monkeypatch.setattr(adapter.files, "write", crash)
    with pytest.raises(OSError, match="simulated crash"):
        run("2026-11-26T18:00")
    monkeypatch.setattr(adapter.files, "write", write)
    result = run("2026-11-26T18:01")
    close = adapter.files.read(book / "sessions" / DAYS[1] / "close.json")
    assert len(close["operator_state"]["fills"]) == 1
    assert result["positions"][CODE]["entry_price"] == pytest.approx(1001)


def test_verified_no_trade_preserves_position_and_unverified_nulls_retry(setup):
    book, run, _, loader = setup
    run("2026-11-25T18:00")
    run("2026-11-26T18:00")
    loader.execution["date"] = DAYS[2]
    row = loader.execution["bars"][CODE]
    row.update(open=None, high=None, low=None, close=None)
    pending = run("2026-11-27T18:00")
    assert pending["status"] == "waiting_close_inputs" and pending["last_mark"] == DAYS[1]
    row["no_trade"] = True
    done = run("2026-11-27T18:01")
    assert done["last_mark"] == DAYS[2] and done["positions"][CODE]["last_mark"] == 1000


def test_gap_down_protective_stop_uses_open_and_fees(setup):
    book, run, _, loader = setup
    run("2026-11-25T18:00")
    run("2026-11-26T18:00")
    loader.execution.update(date=DAYS[2])
    loader.execution["bars"][CODE].update(open=850, high=880, low=830, close=860)
    result = run("2026-11-27T18:00")
    assert result["positions"] == {}
    close = adapter.files.read(book / "sessions" / DAYS[2] / "close.json")
    sold = [
        f for rows in close["operator_state"]["fills"].values() for f in rows if f["side"] == "SELL"
    ]
    assert sold[0]["price"] == pytest.approx(850 * 0.999)
    assert len(close["operator_state"]["receipts"]) == 2


def test_one_third_split_uses_source_precision_and_preserves_acquisition_cost(setup):
    book, run, _, loader = setup
    run("2026-11-25T18:00")
    before = run("2026-11-26T18:00")
    loader.execution["date"] = DAYS[2]
    loader.execution["bars"][CODE].update(
        open=334, high=350, low=330, close=340, factor=0.3333333, share_factor=0.3333333
    )
    after = run("2026-11-27T18:00")
    old, new = before["positions"][CODE], after["positions"][CODE]
    assert new["shares"] == old["shares"] * 3
    assert new["cost"] == pytest.approx(old["cost"], rel=1e-6)
    assert new["entry_date"] == old["entry_date"]


def test_share_exchange_carries_odd_lot_then_stops_board_lots_only(setup):
    book, run, _, loader = setup
    run("2026-11-25T18:00")
    before = run("2026-11-26T18:00")
    held = before["positions"][CODE]
    ratio = 1.25
    successor = "99990"
    loader.execution["date"] = DAYS[2]
    loader.execution["bars"] = {
        successor: dict(
            name="successor",
            sector33="1000",
            open=800,
            high=830,
            low=700,
            close=810,
            factor=1,
            share_factor=1,
        )
    }
    loader.execution["exchanges"] = [
        dict(
            old_code=CODE,
            new_code=successor,
            ratio=ratio,
            effective=DAYS[2],
            known_on=DAYS[0],
            source="fixture verified terms",
        )
    ]
    after = run("2026-11-27T18:00")
    remainder = int(held["shares"] * ratio) % 100
    assert remainder > 0 and after["positions"][successor]["shares"] == remainder
    assert after["positions"][successor]["entry_date"] == held["entry_date"]
    assert after["positions"][successor]["cost"] == pytest.approx(
        held["cost"] * remainder / (held["shares"] * ratio)
    )


def test_computation_crossing_deadline_discards_new_discretionary_exit(setup):
    book, run, source, _ = setup
    run("2026-11-25T18:00")
    run("2026-11-26T18:00")
    close = adapter.files.read(book / "sessions" / DAYS[1] / "close.json")
    source["bars"][CODE]["sma20"] = 1050
    ticks = iter([instant("2026-11-27T08:59:59"), instant("2026-11-27T09:00")])
    decision = adapter.freeze(
        book.parent,
        book,
        close["operator_state"],
        close["snapshot"],
        DAYS[1],
        DAYS[2],
        DAYS,
        lambda: next(ticks),
        3_000_000,
    )
    assert decision["state"] == "missed_preopen"
    assert decision["plan"]["sells"] == decision["plan"]["buys"] == []


def test_missed_initial_decision_does_not_fabricate_market_data(setup):
    book, run, _, _ = setup
    run("2026-11-26T18:00")
    decision = adapter.files.read(book / "sessions" / DAYS[1] / "decision.json")
    assert decision["snapshot"]["topix_close"] is None
    assert decision["topix_bullish"] is None and not decision["allow_entries"]


def test_preopen_stop_tampering_in_completed_session_is_detected(setup):
    book, run, _, _ = setup
    run("2026-11-25T18:00")
    (book / "STOP").touch()
    run("2026-11-26T08:00")
    (book / "STOP").unlink()
    run("2026-11-26T18:00")
    path = book / "sessions" / DAYS[1] / "preopen_stop.json"
    notice = adapter.files.read(path)
    notice["recorded_at"] = "2026-11-26T08:01:00+09:00"
    adapter.files.write(path, notice)
    with pytest.raises(ValueError, match="pead_immutable_preopen_stop_mismatch"):
        run("2026-11-26T18:01")


def prepare_incomplete_bullish_exit(setup):
    book, run, source, loader = setup
    run("2026-11-25T18:00")
    source["audit"]["financial_complete"] = False
    source["bars"][CODE]["sma20"] = 1100
    pending = run("2026-11-26T18:00")
    assert pending["status"] == "waiting_inputs"
    folder = book / "sessions" / DAYS[2]
    evidence = adapter.files.read(folder / "exit-decision.json")
    assert evidence["state"] == "exits_only"
    assert evidence["plan"]["sells"][0]["reason"] == "CLOSE_BELOW_SMA20"
    assert not evidence["picks"] and not (folder / "decision.json").exists()
    loader.execution["date"] = DAYS[2]
    return folder


def test_missing_financials_preserve_exact_preopen_exit_after_late_repair(setup):
    book, run, source, _ = setup
    folder = prepare_incomplete_bullish_exit(setup)
    original = (folder / "exit-decision.json").read_bytes()
    source["audit"]["financial_complete"] = True
    result = run("2026-11-27T18:00")
    assert result["positions"] == {} and result["last_mark"] == DAYS[2]
    assert (folder / "decision.json").read_bytes() == original
    close = adapter.files.read(folder / "close.json")
    fills = [f for rows in close["operator_state"]["fills"].values() for f in rows]
    assert len(fills) == 1 and fills[0]["side"] == "SELL"


def test_timely_financial_recovery_can_seal_new_buys_before_auction(setup):
    book, run, source, _ = setup
    folder = prepare_incomplete_bullish_exit(setup)
    original = (folder / "exit-decision.json").read_bytes()
    source["audit"]["financial_complete"] = True
    second = "23450"
    source["bars"][second] = dict(source["bars"][CODE], sma20=950)
    source["candidates"] = [second]
    ready = run("2026-11-27T08:45")
    assert ready["orders"] == 1 and ready["allow_entries"]
    final = adapter.files.read(folder / "decision.json")
    assert final["picks"][0]["code"] == second
    assert final["plan"]["sells"][0]["code"] == CODE
    assert (folder / "exit-decision.json").read_bytes() == original


def test_no_preopen_exit_evidence_never_infers_discretionary_exit_after_open(setup):
    book, run, source, loader = setup
    run("2026-11-25T18:00")
    run("2026-11-26T18:00")
    folder = book / "sessions" / DAYS[2]
    (folder / "decision.json").unlink()
    source["bars"][CODE]["sma20"] = 1100
    loader.execution["date"] = DAYS[2]
    result = run("2026-11-27T18:00")
    decision = adapter.files.read(folder / "decision.json")
    assert decision["state"] == "missed_preopen"
    assert decision["plan"]["buys"] == decision["plan"]["sells"] == []
    assert CODE in result["positions"]


def test_spinoff_entitlement_review_persists_without_cash_credit_or_new_gate(setup):
    book, run, _, loader = setup
    run("2026-11-25T18:00")
    before = run("2026-11-26T18:00")
    loader.execution["date"] = DAYS[2]
    loader.execution["bars"][CODE].update(
        open=900, high=930, low=880, close=910, factor=0.9, share_factor=1, spinoff=True
    )
    after = run("2026-11-27T18:00")
    assert after["status"] == "needs_review"
    assert after["review_status"] == "needs_entitlement_review"
    assert after["positions"][CODE]["shares"] == before["positions"][CODE]["shares"]
    assert after["cash"] == before["cash"]
    assert not after["valuation_complete"] and after["entitlement_review"][0]["date"] == DAYS[2]
    repeated = run("2026-11-27T18:01")
    assert repeated["entitlement_review"] == after["entitlement_review"]
