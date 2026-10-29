"""No real sockets: common-contract acquisition, failure recovery and persistence."""

import base64
import json
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from trading_system.brokers.base import BrokerAdapter, Unavailable
from trading_system.brokers.locking import single_writer
from trading_system.brokers.market_data import run
from trading_system.brokers.models import Account
from trading_system.brokers.mufg_esmart.adapter import Adapter
from trading_system.brokers.mufg_esmart.capture import main
from trading_system.brokers.mufg_esmart.market_data import Client
from trading_system.brokers.registry import vendor_adapter
from trading_system.storage.market_data import MarketDataStore

PASSWORD = "MockPass456"
TOKEN = "offline-fixture-token"


class Clock:
    def __init__(self, value="2026-10-29T10:00:00+09:00"):
        self.value = datetime.fromisoformat(value)

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += timedelta(seconds=seconds)


def board(clock):
    value = {
        "Symbol": "7203",
        "Exchange": 1,
        "CurrentPrice": 100,
        "CurrentPriceTime": clock.now().isoformat(),
    }
    for index in range(1, 11):
        value[f"Buy{index}"] = {"Price": 100 - index, "Qty": 100.0}
        value[f"Sell{index}"] = {"Price": 100 + index, "Qty": 200.0}
    return value


def setup(clock, *, replies=None, auth_status=200):
    seen = []
    replies = list(replies or [])

    def handle(request):
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            assert json.loads(request.content) == {"APIPassword": PASSWORD}
            return httpx.Response(auth_status, json={"ResultCode": 0, "Token": TOKEN})
        assert request.headers["X-API-KEY"] == TOKEN
        response = replies.pop(0) if replies else board(clock)
        if response == "disconnect":
            raise httpx.ConnectError(PASSWORD, request=request)
        if isinstance(response, int):
            return httpx.Response(response, json={"Code": 4001009})
        return httpx.Response(200, content=json.dumps(response).encode())

    client = Client(
        PASSWORD, transport_factory=lambda: httpx.MockTransport(handle), clock=clock.now
    )
    adapter = Adapter(
        Account(broker="mufg_esmart", account_id="test"),
        enabled=True,
        market_data_client=client,
        clock=clock.now,
    )
    return adapter, seen


def ready(adapter):
    adapter.refresh()  # token
    for _ in range(3):
        adapter.refresh()


def test_common_contract_mapping_causality_and_no_order_transport():
    clock = Clock()
    adapter, seen = setup(clock)
    assert isinstance(adapter, BrokerAdapter)
    ready(adapter)
    connection = adapter.connection_status()
    assert connection.state == "read_only_ready" and connection.market_data_verified
    assert not connection.real_execution_enabled and not connection.live_verified
    quote = adapter.quote("7203", clock.now())
    book = adapter.board("7203", clock.now())
    assert quote.evidence == "observed" and not quote.freshness_verified
    assert quote.known_at == clock.now() == quote.source_updated_at
    assert book.bids[0].price == 99 and book.asks[0].price == 101
    assert book.bids[0].quantity == 100
    before = len(seen)
    for action in (
        lambda: adapter.submit(None, clock.now()),
        lambda: adapter.cancel("x", clock.now()),
        lambda: adapter.modify("x", Decimal(1), 1, clock.now()),
        lambda: adapter.orders(clock.now()),
        lambda: adapter.balance(clock.now()),
    ):
        with pytest.raises(Unavailable):
            action()
    assert len(seen) == before
    with pytest.raises(Unavailable):
        adapter.quote("7203", clock.now() - timedelta(seconds=1))
    clock.sleep(61)
    with pytest.raises(Unavailable):
        adapter.quote("7203", clock.now())
    adapter.close()
    assert adapter.client._password is None and adapter.client._token is None


def test_disconnect_discards_cache_and_requires_three_new_valid_reads():
    clock = Clock()
    adapter, _ = setup(clock, replies=[board(clock)] * 3 + ["disconnect"])
    ready(adapter)
    record, raw = adapter.refresh()
    assert record["reason"] == "network_request_failed" and record["retryable"]
    assert raw is None and adapter.connection_status().state == "reconnecting"
    with pytest.raises(Unavailable):
        adapter.quote("7203", clock.now())
    for _ in range(2):
        adapter.refresh()
        with pytest.raises(Unavailable):
            adapter.board("7203", clock.now())
    adapter.refresh()
    assert adapter.quote("7203", clock.now()).price == 100
    adapter.close()


@pytest.mark.parametrize(
    "bad",
    [
        "stale",
        "future",
        "missing",
        "zero_qty",
        "fraction_qty",
        "crossed",
        "wrong_symbol",
        "regressed",
        "null_level",
    ],
)
def test_bad_data_keeps_raw_and_blocks_common_access(bad):
    clock = Clock()
    payload = board(clock)
    if bad in ("stale", "future", "regressed"):
        offset = {"stale": -86400, "future": 60, "regressed": -1}[bad]
        payload["CurrentPriceTime"] = (clock.now() + timedelta(seconds=offset)).isoformat()
    elif bad == "missing":
        payload["CurrentPrice"] = None
    elif bad == "zero_qty":
        payload["Buy1"]["Qty"] = 0
    elif bad == "fraction_qty":
        payload["Buy1"]["Qty"] = 1.5
    elif bad == "crossed":
        payload["Buy1"]["Price"] = 999
    elif bad == "null_level":
        payload["Sell2"] = None
    else:
        payload["Symbol"] = "9999"
    adapter, _ = setup(clock, replies=[board(clock)] * 3 + [payload])
    ready(adapter)
    record, raw = adapter.refresh()
    assert json.loads(raw) == payload
    assert record["issues"] and not record["accepted"]
    assert record["common_quote"] is None and record["common_board"] is None
    with pytest.raises(Unavailable):
        adapter.quote("7203", clock.now())
    adapter.close()


@pytest.mark.parametrize("status", [401, 403])
def test_token_loss_reauthenticates_only_once(status):
    clock = Clock()
    adapter, seen = setup(clock, replies=[status, status])
    adapter.refresh()
    record, _ = adapter.refresh()
    assert record["reason"] == "token_expired" and record["retryable"]
    adapter.refresh()
    record, _ = adapter.refresh()
    assert record["outcome"] == "auth_required" and not record["retryable"]
    assert sum(method == "POST" for method, _ in seen) == 2
    adapter.close()


def test_wrong_password_never_retries_or_fetches_board(tmp_path):
    clock = Clock()
    adapter, seen = setup(clock, auth_status=401)
    result = run(tmp_path, lambda: adapter, clock=clock.now, sleep=clock.sleep, max_seconds=60)
    assert seen == [("POST", "/kabusapi/token")]
    assert not result["normal_exit"] and result["stop_reason"] == "auth_required"
    assert result["data_consistency_verified"]
    for path in (tmp_path / "raw").glob("*/*.json"):
        assert PASSWORD not in path.read_text() and TOKEN not in path.read_text()


def test_live_registry_requires_explicit_client_and_enabled_flag():
    account = Account(broker="mufg_esmart", account_id="test")
    assert vendor_adapter(account).connection_status().state == "disabled"
    assert vendor_adapter(account, enabled=True).connection_status().state == "not_implemented"
    adapter, _ = setup(Clock())
    with pytest.raises(ValueError, match="disabled"):
        vendor_adapter(account, market_data_client=adapter.client)
    with pytest.raises(Unavailable):
        vendor_adapter(
            Account(broker="rakuten", account_id="test"),
            enabled=True,
            market_data_client=adapter.client,
        )
    adapter.close()


def test_runner_recovery_and_complete_raw_db_parquet_preservation(tmp_path):
    clock = Clock()
    adapter, seen = setup(clock, replies=[board(clock)] * 4 + ["disconnect"] * 2)
    result = run(tmp_path, lambda: adapter, clock=clock.now, sleep=clock.sleep, max_seconds=90)
    assert result["normal_exit"] and result["data_consistency_verified"]
    assert result["gaps"] == result["recoveries"] == 1
    assert result["accepted"] > 1 and result["common_access_verified"]
    assert result["order_requests"] == 0 and not result["real_execution_enabled"]
    assert result["stop_reason"] == "duration_complete"
    assert all(path in ("/kabusapi/token", "/kabusapi/board/7203@1") for _, path in seen)
    store = MarketDataStore(tmp_path)
    try:
        assert store.audit()["observations"] == result["samples"]
        relations = {
            row[0] for row in store.db.execute("SELECT relation FROM observations").fetchall()
        }
        assert {"no_payload", "identical", "changed_snapshot_revision_unverified"} <= relations
        assert store.export()["parquet_matches"]
        before = store.audit()
        store.recover()
        assert store.audit() == before
    finally:
        store.close()
    for path in (tmp_path / "raw").glob("*/*.json"):
        envelope = json.loads(path.read_bytes())
        raw = base64.b64decode(envelope["body_base64"] or "")
        assert PASSWORD.encode() not in raw and TOKEN.encode() not in raw


@pytest.mark.parametrize("clock_value", ["2026-10-29T16:00:00+09:00", "2026-10-29T19:00:00+09:00"])
def test_after_deadline_never_prompts_or_calls_api(tmp_path, clock_value):
    clock = Clock(clock_value)

    def forbidden():
        pytest.fail("No factory after 16:00")

    result = run(tmp_path, forbidden, clock=clock.now, sleep=clock.sleep)
    assert result["normal_exit"] and result["samples"] == 0
    assert result["stop_reason"] == "daytime_1600"


def test_stop_wait_and_overnight_wake_keep_original_deadline(tmp_path):
    clock = Clock("2026-10-29T15:59:58+09:00")
    adapter, seen = setup(clock)

    def overnight(_):
        clock.sleep(86400)

    result = run(tmp_path, lambda: adapter, clock=clock.now, sleep=overnight)
    assert result["stop_reason"] == "daytime_1600" and len(seen) == 1
    assert "2026-10-29" in result["scheduled_stop_at"]


def test_operator_stop_during_retry_does_not_read_again(tmp_path):
    clock = Clock()
    adapter, seen = setup(clock, replies=["disconnect"])

    def stop(seconds):
        clock.sleep(seconds)
        if len(seen) == 2:
            (tmp_path / "STOP").touch()

    result = run(tmp_path, lambda: adapter, clock=clock.now, sleep=stop)
    assert result["stop_reason"] == "operator_stop" and len(seen) == 2
    assert (tmp_path / "STOP").exists()


def test_retry_backoff_exhaustion_and_rate_limit(tmp_path):
    clock = Clock()
    adapter, seen = setup(clock, replies=[429] * 6)
    result = run(tmp_path, lambda: adapter, clock=clock.now, sleep=clock.sleep, max_seconds=1000)
    assert not result["normal_exit"] and result["stop_reason"] == "retry_limit"
    assert len(seen) == 7
    assert (clock.now() - datetime.fromisoformat(result["started_at"])).total_seconds() >= 155


def test_lock_prevents_second_prompt_and_capture(tmp_path):
    with single_writer(tmp_path):
        with pytest.raises(ValueError, match="already_open"):
            run(tmp_path, lambda: pytest.fail("Second writer prompted"))


def observation(clock):
    adapter, _ = setup(clock)
    adapter.refresh()
    result = adapter.refresh()
    adapter.close()
    return result


def test_transaction_failure_retains_raw_and_recovers_checkpoint(tmp_path):
    store = MarketDataStore(tmp_path)
    actual_db = store.db

    class FailCheckpoint:
        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO checkpoints"):
                raise RuntimeError("disk failure")
            return actual_db.execute(sql, *args)

    store.db = FailCheckpoint()
    record, raw = observation(Clock())
    with pytest.raises(RuntimeError):
        store.save("a" * 32, 1, record, raw)
    assert actual_db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
    assert len(list((tmp_path / "raw").glob("*/*.json"))) == 1
    store.db = actual_db
    store.recover()
    assert store.audit()["observations"] == 1
    assert store.db.execute("SELECT sample FROM checkpoints").fetchone()[0] == 1
    store.close()


def test_raw_tampering_and_identity_rewrite_are_rejected(tmp_path):
    store = MarketDataStore(tmp_path)
    record, raw = observation(Clock())
    run_id = "b" * 32
    store.save(run_id, 1, record, raw)
    with pytest.raises(ValueError, match="conflict"):
        store.save(run_id, 1, record, b"changed")
    path = next((tmp_path / "raw").glob("*/*.json"))
    content = json.loads(path.read_bytes())
    content["record"]["accepted"] = True
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError, match="content_mismatch"):
        store.export()
    store.close()


def test_export_failure_never_reports_normal_exit(tmp_path, monkeypatch):
    clock = Clock()
    adapter, _ = setup(clock)

    def fail(_):
        raise OSError(PASSWORD)

    monkeypatch.setattr(MarketDataStore, "export", fail)
    result = run(tmp_path, lambda: adapter, clock=clock.now, sleep=clock.sleep, max_seconds=10)
    assert not result["normal_exit"] and not result["data_consistency_verified"]
    assert result["stop_reason"] == "save_or_audit_failed"
    assert PASSWORD not in (tmp_path / "status.json").read_text()


def test_cli_requires_mode_and_supports_offline_stop(tmp_path, capsys):
    assert main(["--root", str(tmp_path)]) == 2
    assert main(["--root", str(tmp_path), "--stop"]) == 0
    assert (tmp_path / "STOP").exists()
    assert "stop_requested" in capsys.readouterr().out


@pytest.mark.parametrize("status", [500, 503])
def test_temporary_server_failure_recovers_with_existing_token(tmp_path, status):
    clock = Clock()
    adapter, seen = setup(clock, replies=[status])
    result = run(tmp_path, lambda: adapter, clock=clock.now, sleep=clock.sleep, max_seconds=40)
    assert result["normal_exit"] and result["recoveries"] == 1
    assert sum(method == "POST" for method, _ in seen) == 1


def test_reauthentication_requires_new_valid_data_before_resume(tmp_path):
    clock = Clock()
    adapter, seen = setup(clock, replies=[board(clock)] * 3 + [401])
    result = run(tmp_path, lambda: adapter, clock=clock.now, sleep=clock.sleep, max_seconds=60)
    assert result["normal_exit"] and result["recoveries"] == 1
    assert sum(method == "POST" for method, _ in seen) == 2
    observations = [
        json.loads(p.read_bytes())["record"] for p in sorted((tmp_path / "raw").glob("*/*.json"))
    ]
    expired_index = next(i for i, r in enumerate(observations) if r["http_status"] == 401)
    assert not any(r["accepted"] for r in observations[expired_index : expired_index + 4])
    assert observations[expired_index + 4]["accepted"]


def test_read_crossing_deadline_is_saved_without_another_request(tmp_path):
    clock = Clock("2026-10-29T15:59:59+09:00")
    adapter, seen = setup(clock)
    original = adapter.refresh

    def slow():
        result = original()
        clock.sleep(2)
        return result

    adapter.refresh = slow
    result = run(tmp_path, lambda: adapter, clock=clock.now, sleep=clock.sleep)
    assert result["normal_exit"] and result["stop_reason"] == "daytime_1600"
    assert len(seen) == 1 and result["final_export"]["observations"] == 1


def test_prompt_crossing_deadline_never_authenticates(tmp_path):
    clock = Clock("2026-10-29T15:59:59+09:00")
    adapter, seen = setup(clock)

    def delayed_factory():
        clock.sleep(5)
        return adapter

    result = run(tmp_path, delayed_factory, clock=clock.now, sleep=clock.sleep)
    assert result["normal_exit"] and not seen and adapter.client.closed


@pytest.mark.parametrize(
    "field,value",
    [("relation", "identical"), ("broker", "rakuten"), ("code", "9999"), ("operation", "token")],
)
def test_catalog_metadata_corruption_prevents_publication(tmp_path, field, value):
    store = MarketDataStore(tmp_path)
    record, raw = observation(Clock())
    store.save("c" * 32, 1, record, raw)
    store.db.execute(f"UPDATE observations SET {field}=?", [value])
    with pytest.raises(ValueError, match="content_mismatch"):
        store.export()
    assert not (tmp_path / "parquet" / "current.json").exists()
    store.close()


def test_offline_recovery_does_not_synthesize_old_session_completion(tmp_path):
    store = MarketDataStore(tmp_path)
    record, raw = observation(Clock())
    store.save("d" * 32, 1, record, raw)
    store.close()
    clock = Clock("2026-10-29T17:00:00+09:00")
    result = run(
        tmp_path, lambda: pytest.fail("Recovery contacted API"), clock=clock.now, sleep=clock.sleep
    )
    assert result["normal_exit"] and result["final_export"]["observations"] == 1
    assert not (tmp_path / "sessions" / (("d" * 32) + ".json")).exists()


def test_recovery_probe_is_local_and_explicitly_counted(monkeypatch):
    from trading_system.brokers.mufg_esmart.market_data import RecoveryProbe

    class Inner(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(200, json={})

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kwargs: Inner())
    probe = RecoveryProbe(after=1, failures=2)
    for expected_failure in (False, True, True, False):
        with httpx.Client(transport=probe.transport()) as http:
            if expected_failure:
                with pytest.raises(httpx.ConnectError):
                    http.get("http://localhost:18080/kabusapi/board/7203@1")
            else:
                assert http.get("http://localhost:18080/kabusapi/board/7203@1").status_code == 200
            assert probe.just_injected is expected_failure
    assert probe.injected == 2


def test_sensitive_board_response_never_enters_raw(tmp_path):
    clock = Clock()
    payload = board(clock)
    payload["extra"] = TOKEN
    adapter, _ = setup(clock, replies=[payload])
    result = run(tmp_path, lambda: adapter, clock=clock.now, sleep=clock.sleep, max_seconds=30)
    assert not result["normal_exit"] and result["last_reason"] == "sensitive_response_not_saved"
    for path in (tmp_path / "raw").glob("*/*.json"):
        envelope = json.loads(path.read_bytes())
        assert envelope["body_base64"] is None and TOKEN not in path.read_text()
