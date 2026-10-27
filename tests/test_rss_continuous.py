import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_system import rss_continuous
from trading_system.data_sources.rss_live import observe, validate_read
from trading_system.data_sources.rss_snapshot import ITEMS
from trading_system.paper_rss import CONFIG, PaperLedger

NOW = datetime(2026, 1, 5, 1, tzinfo=UTC)
CODE = "7203.T"


def raw(t=NOW, price=100):
    formulas = [f'=RssMarket("{CODE}","{item}")' for item in ITEMS.values()]
    values = [price, (t + timedelta(hours=9)).strftime("%H:%M:%S"), t.strftime("%Y/%m/%d")]
    return dict(
        schema_version=1,
        status="read",
        formulas_before=formulas,
        formulas_after=formulas,
        values_before=values,
        values_after=values,
    )


def body(t=NOW, price=100, error=None, quote_at=None):
    source = {"status": "error", "reason": error} if error else raw(quote_at or t, price)
    record = validate_read(source, CODE, t)
    record.update(
        read_started_at=t.isoformat(),
        read_completed_at=t.isoformat(),
        observed_at=t.isoformat(),
        raw=source,
    )
    return json.dumps(record).encode()


def feed(ledger, n, price=100, **kwargs):
    t = NOW + timedelta(seconds=n)
    return ledger.consume(str(n), body(t, price, **kwargs), t)


def test_live_values_roundtrip_costs_and_restart_idempotency(tmp_path):
    path = tmp_path / "paper.sqlite"
    ledger = PaperLedger(path, CODE)
    feed(ledger, 0, 100)
    feed(ledger, 1, 100)  # next quote buys, adverse slippage + fixed fee
    ledger.close()
    ledger = PaperLedger(path, CODE)
    assert feed(ledger, 1, 100) is False
    feed(ledger, 2, 110)  # next signal, no same-tick fill
    feed(ledger, 3, 110)
    report = ledger.snapshot()
    assert report["completed_roundtrip"] and report["accounting_matches"]
    assert report["state"]["fills"] == 2 and report["state"]["position"] == 0
    assert Decimal(report["realized_test_pnl"]) == Decimal("969.50")
    for n in range(4, 10):
        feed(ledger, n, 120)
    assert ledger.snapshot()["state"]["fills"] == 2
    ledger.close()


@pytest.mark.parametrize("error", ["excel_not_ready", "excel_not_registered", "excel_read_timeout"])
def test_read_failure_cancels_pending_and_recovers_without_fake_fill(tmp_path, error):
    ledger = PaperLedger(tmp_path / "paper.sqlite", CODE)
    feed(ledger, 0)
    feed(ledger, 1, error=error)
    feed(ledger, 2)
    assert ledger.snapshot()["state"]["fills"] == 0
    feed(ledger, 3)
    assert ledger.snapshot()["state"]["fills"] == 1
    ledger.close()


def test_repeated_or_stale_quote_never_fills(tmp_path):
    ledger = PaperLedger(tmp_path / "paper.sqlite", CODE)
    feed(ledger, 0)
    for n in (1, 2, 30, 31, 40):
        feed(ledger, n, quote_at=NOW)
    assert ledger.snapshot()["state"]["fills"] == 0
    assert ledger.snapshot()["state"]["phase"] == "buy_signal"
    ledger.close()


def test_recovery_never_retroactively_fills(tmp_path):
    ledger = PaperLedger(tmp_path / "paper.sqlite", CODE)
    feed(ledger, 0)
    t = NOW + timedelta(seconds=1)
    ledger.consume("recover", body(t), t, recovery=True)
    assert ledger.snapshot()["state"]["fills"] == 0
    feed(ledger, 2)
    assert ledger.snapshot()["state"]["fills"] == 0
    ledger.close()


def test_corrupted_normalized_price_not_used_and_identity_conflict_rejected(tmp_path):
    ledger = PaperLedger(tmp_path / "paper.sqlite", CODE)
    feed(ledger, 0)
    t = NOW + timedelta(seconds=1)
    record = json.loads(body(t))
    record["normalized"]["price"] = "999999"
    ledger.consume("one", json.dumps(record).encode(), t)
    assert ledger.snapshot()["fill_events"][0]["price"] == "100.0500"
    with pytest.raises(ValueError, match="identity_conflict"):
        ledger.consume("one", body(t, 101), t)
    ledger.close()


def test_old_future_wrong_code_and_out_of_order_do_not_fill(tmp_path):
    ledger = PaperLedger(tmp_path / "paper.sqlite", CODE)
    feed(ledger, 2)
    feed(ledger, 1)
    ledger.consume("old", body(NOW), NOW + timedelta(minutes=5))
    ledger.consume("future", body(NOW + timedelta(minutes=1)), NOW)
    record = json.loads(body())
    record["code"] = "1234.T"
    ledger.consume("wrong", json.dumps(record).encode(), NOW)
    assert ledger.snapshot()["state"]["fills"] == 0
    ledger.close()


def test_cash_limit_and_config_immutable(tmp_path):
    path = tmp_path / "paper.sqlite"
    ledger = PaperLedger(path, CODE)
    feed(ledger, 0, 100000)
    feed(ledger, 1, 100000)
    assert ledger.snapshot()["state"]["fills"] == 0
    ledger.close()
    with pytest.raises(ValueError, match="configuration_changed"):
        PaperLedger(path, "1234.T")


def test_single_writer_lock_released(tmp_path):
    with rss_continuous.single_writer(tmp_path):
        with pytest.raises(ValueError, match="already_running"):
            with rss_continuous.single_writer(tmp_path):
                pytest.fail("second writer acquired")
    with rss_continuous.single_writer(tmp_path):
        pass


def test_continuous_restart_stop_and_all_raw_imported(tmp_path):
    root = tmp_path / "continuous"
    count = 0

    def observer(workbook, code, runs_root, **kwargs):
        nonlocal count
        count += 1
        current = datetime.now(UTC)
        result = observe(
            workbook,
            code,
            runs_root,
            samples=1,
            reader=lambda *_: raw(current),
            utcnow=lambda: current,
        )
        if count == 2:
            (root / "STOP").touch()
        return result

    first = rss_continuous.run(
        utcnow=lambda: NOW,
        workbook=tmp_path / "test.xlsx",
        code=CODE,
        root=root,
        paper=True,
        observer=observer,
    )
    assert first["cycles_completed"] == 2 and first["stop_reason"] == "stop_file"
    assert first["coverage"]["unimported_samples"] == 0
    assert first["audit"]["observations"] == 2
    with pytest.raises(ValueError, match="stop_requested"):
        rss_continuous.run(
            workbook=tmp_path / "test.xlsx", code=CODE, root=root, paper=True, utcnow=lambda: NOW
        )
    (root / "STOP").unlink()
    second = rss_continuous.run(
        utcnow=lambda: NOW,
        workbook=tmp_path / "test.xlsx",
        code=CODE,
        root=root,
        paper=True,
        cycles=1,
        observer=observer,
    )
    assert second["paper"]["source_records"] == 3
    assert second["recovered_samples"] == 0
    assert second["paper"]["accounting_matches"]
    assert second["audit"]["observations"] == 3


def test_partial_run_after_interruption_preserved_and_imported(tmp_path):
    root = tmp_path / "continuous"

    def interrupt(workbook, code, runs_root, **kwargs):
        current = datetime.now(UTC)
        folder, _ = observe(
            workbook,
            code,
            runs_root,
            samples=1,
            reader=lambda *_: raw(current),
            utcnow=lambda: current,
        )
        (folder / "summary.json").unlink()  # models Ctrl+C after source write
        raise KeyboardInterrupt

    result = rss_continuous.run(
        utcnow=lambda: NOW,
        workbook=tmp_path / "test.xlsx",
        code=CODE,
        root=root,
        paper=True,
        observer=interrupt,
    )
    assert result["stop_reason"] == "keyboard_interrupt"
    assert result["coverage"]["unimported_samples"] == 0
    assert "run_summary_missing" in result["coverage"]["issues"]
    assert result["paper"]["state"]["fills"] == 0


def test_failed_capture_halts_and_saves_status(tmp_path):
    def fail(*args, **kwargs):
        raise OSError("fake disk failure")

    with pytest.raises(OSError):
        rss_continuous.run(
            utcnow=lambda: NOW,
            workbook=tmp_path / "test.xlsx",
            code=CODE,
            root=tmp_path / "root",
            observer=fail,
        )
    report = json.loads((tmp_path / "root/status.json").read_bytes())
    assert report["status"] == "failed" and report["real_execution_enabled"] is False


def test_config_never_enables_real_execution():
    assert CONFIG["scenario"] == "one_fixed_roundtrip_connectivity_test"
