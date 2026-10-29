"""Offline request-boundary, credential handling, and snapshot checks."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from trading_system.brokers.mufg_esmart import connection_check as probe

PASSWORD = "MockPass123"
TOKEN = "fixture-token-only-for-offline-tests"


def board():
    payload = {
        "Symbol": "7203",
        "Exchange": 1,
        "CurrentPrice": 100,
        "CurrentPriceTime": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    }
    for side in ("Sell", "Buy"):
        for index in range(1, 11):
            payload[f"{side}{index}"] = {"Price": 100, "Qty": 100}
    return payload


def transport(payload, *, auth_status=200, board_status=200, token=TOKEN):
    seen = []

    def handle(request):
        seen.append((request.method, str(request.url)))
        if request.url.path == "/kabusapi/token":
            assert json.loads(request.content) == {"APIPassword": PASSWORD}
            return httpx.Response(auth_status, json={"ResultCode": 0, "Token": token})
        assert request.headers["X-API-KEY"] == TOKEN
        return httpx.Response(board_status, content=payload)

    return httpx.MockTransport(handle), seen


def test_auth_and_board_only_preserve_raw_without_credentials(tmp_path):
    raw = json.dumps(board(), indent=2).encode()
    mock, seen = transport(raw)
    run, result = probe.check_connection(PASSWORD, tmp_path, transport=mock)
    assert seen == [("POST", probe.BASE + "/token"), ("GET", probe.BASE + probe.BOARD_PATH)]
    assert result["status"] == "connected" and result["api_authenticated"]
    assert result["quality"]["valid_levels"] == {"sell": 10, "buy": 10}
    assert not result["quality"]["freshness_verified"]
    assert result["quality"]["source_updated_at"] is None
    assert result["order_requests"] == 0 and not result["real_execution_enabled"]
    assert (run / "board.raw.json").read_bytes() == raw
    assert result["raw_sha256"] == probe.digest(raw)
    assert json.loads((run / "report.json").read_bytes()) == result
    for path in run.iterdir():
        assert (
            PASSWORD.encode() not in path.read_bytes() and TOKEN.encode() not in path.read_bytes()
        )


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", probe.BASE + "/sendorder"),
        ("PUT", probe.BASE + "/cancelorder"),
        ("GET", probe.BASE + "/orders"),
        ("GET", probe.BASE + "/wallet/cash"),
        ("GET", "http://example.invalid/kabusapi/board/7203@1"),
        ("GET", "http://localhost:18081/kabusapi/board/7203@1"),
        ("GET", probe.BASE + "/board/7203@1?extra=1"),
        ("POST", probe.BASE + "/board/7203@1"),
    ],
)
def test_guard_rejects_unapproved_requests_before_network(method, url):
    def forbidden(_):
        pytest.fail("Blocked request reached the network transport")

    with httpx.Client(transport=probe.ReadOnlyTransport(httpx.MockTransport(forbidden))) as client:
        with pytest.raises(probe.ProbeError, match="request_not_allowed"):
            client.request(method, url)


@pytest.mark.parametrize("auth_status", [401, 403, 429, 500, 302])
def test_failed_auth_does_not_retry_fetch_board_or_save_token(tmp_path, auth_status):
    mock, seen = transport(b"{}", auth_status=auth_status)
    run, result = probe.check_connection(PASSWORD, tmp_path, transport=mock)
    assert len(seen) == 1 and result["status"] == "failed"
    assert result["reason"] == f"token_http_{auth_status}"
    assert list(run.iterdir()) == [run / "report.json"]
    assert TOKEN not in (run / "report.json").read_text()


@pytest.mark.parametrize("payload", [b"not json", b"[]", b'"value"'])
def test_invalid_board_keeps_raw_and_fails(tmp_path, payload):
    mock, _ = transport(payload)
    run, result = probe.check_connection(PASSWORD, tmp_path, transport=mock)
    assert result["status"] == "failed"
    assert (run / "board.raw.json").read_bytes() == payload


@pytest.mark.parametrize("secret", [PASSWORD, TOKEN])
def test_echoed_secrets_are_never_saved(tmp_path, secret):
    mock, _ = transport(json.dumps({"Message": secret}).encode(), board_status=400)
    run, result = probe.check_connection(PASSWORD, tmp_path, transport=mock)
    assert result["reason"] == "sensitive_response_not_saved"
    assert not (run / "board.raw.json").exists()
    assert secret not in (run / "report.json").read_text()


def test_unicode_escaped_secret_is_not_written_to_raw(tmp_path):
    escaped = "".join(f"\\u{ord(letter):04x}" for letter in PASSWORD)
    mock, _ = transport(('{"Message":"' + escaped + '"}').encode())
    run, result = probe.check_connection(PASSWORD, tmp_path, transport=mock)
    assert result["reason"] == "sensitive_response_not_saved"
    assert not (run / "board.raw.json").exists()


def test_null_prices_are_kept_and_do_not_claim_quote_validity(tmp_path):
    payload = board()
    payload.update(CurrentPrice=None, CurrentPriceTime=None, Sell1=None)
    raw = json.dumps(payload).encode()
    mock, _ = transport(raw)
    run, result = probe.check_connection(PASSWORD, tmp_path, transport=mock)
    assert result["status"] == "connected_with_attention"
    assert "current_price_missing_or_invalid" in result["quality"]["issues"]
    assert result["quality"]["current_price_time"] is None
    assert (run / "board.raw.json").read_bytes() == raw


def test_wrong_symbol_does_not_pass_connection_check(tmp_path):
    payload = board()
    payload["Symbol"] = "9999"
    mock, _ = transport(json.dumps(payload).encode())
    _, result = probe.check_connection(PASSWORD, tmp_path, transport=mock)
    assert result["status"] == "failed" and result["reason"] == "unexpected_symbol_or_exchange"


def test_timeout_does_not_expose_request_or_secret(tmp_path):
    def timeout(request):
        raise httpx.ReadTimeout(PASSWORD, request=request)

    run, result = probe.check_connection(PASSWORD, tmp_path, transport=httpx.MockTransport(timeout))
    assert result["reason"] == "network_request_failed"
    assert PASSWORD not in (run / "report.json").read_text()


def test_response_size_is_bounded(tmp_path):
    mock, _ = transport(b"x" * (probe.MAX_BYTES + 1))
    run, result = probe.check_connection(PASSWORD, tmp_path, transport=mock)
    assert result["reason"] == "response_too_large"
    assert not (run / "board.raw.json").exists()


def test_new_snapshot_does_not_overwrite_previous_version(tmp_path):
    for price in (100, 101):
        payload = board()
        payload["CurrentPrice"] = price
        mock, _ = transport(json.dumps(payload).encode())
        probe.check_connection(PASSWORD, tmp_path, transport=mock)
    assert len(list(tmp_path.glob("*/board.raw.json"))) == 2


def test_cli_cancel_and_missing_prompt_do_not_connect(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(probe, "local_password_prompt", lambda: None)
    assert probe.main(["--root", str(tmp_path)]) == 2
    assert probe.main(["--root", str(tmp_path), "--prompt"]) == 2
    assert not list(tmp_path.iterdir())
    assert "cancelled" in capsys.readouterr().out


def test_cli_output_is_portable_and_credential_free(tmp_path, monkeypatch, capsys):
    root = tmp_path / "接続確認"
    mock, _ = transport(json.dumps(board()).encode())
    original = probe.check_connection
    monkeypatch.setattr(probe, "local_password_prompt", lambda: PASSWORD)
    monkeypatch.setattr(
        probe, "check_connection", lambda password, root: original(password, root, transport=mock)
    )
    assert probe.main(["--root", str(root), "--prompt"]) == 0
    output = capsys.readouterr().out
    assert output.isascii() and PASSWORD not in output and TOKEN not in output
    report = json.loads(output)
    assert report["status"] == "connected"
    assert report["report_path"].startswith(str(root))
