"""One-shot local authentication and board check; no order or account endpoints."""

import argparse
import json
import math
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from trading_system.storage.store import atomic_write, canonical, digest

BASE = "http://localhost:18080/kabusapi"
BOARD_PATH = "/board/7203@1"
ALLOWED = frozenset({("POST", BASE + "/token"), ("GET", BASE + BOARD_PATH)})
MAX_BYTES = 1024 * 1024


class ProbeError(RuntimeError):
    """Messages are fixed local reason codes, never server text or credentials."""


class ReadOnlyTransport(httpx.BaseTransport):
    def __init__(self, inner=None):
        self.inner = inner if inner is not None else httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        if (request.method, str(request.url)) not in ALLOWED:
            raise ProbeError("request_not_allowed")
        return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


def _read(client, method, url, **kwargs):
    started = time.monotonic()
    chunks = []
    size = 0
    with client.stream(method, url, **kwargs) as response:
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise ProbeError("response_too_large")
            if time.monotonic() - started > 15:
                raise ProbeError("response_time_limit")
            chunks.append(chunk)
        return response.status_code, b"".join(chunks)


def _object(body):
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        raise ProbeError("invalid_json_response") from None
    if not isinstance(value, dict):
        raise ProbeError("invalid_response_shape")
    return value


def _contains_secret(body, secrets):
    try:
        decoded = json.dumps(json.loads(body), ensure_ascii=False)
    except (ValueError, UnicodeError):
        decoded = ""
    return any(secret.encode() in body or secret in decoded for secret in secrets)


def _number(value, *, positive=False):
    try:
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and (value > 0 if positive else value >= 0)
        )
    except OverflowError:
        return False


def inspect_board(board, completed):
    issues = []
    if (
        board.get("Symbol") != "7203"
        or type(board.get("Exchange")) is not int
        or board["Exchange"] != 1
    ):
        issues.append("unexpected_symbol_or_exchange")
    if not _number(board.get("CurrentPrice"), positive=True):
        issues.append("current_price_missing_or_invalid")
    quote_time = None
    try:
        value = datetime.fromisoformat(board["CurrentPriceTime"])
        if value.utcoffset() is None or value > completed:
            raise ValueError
        quote_time = value.isoformat()
    except (ValueError, TypeError, KeyError):
        issues.append("current_price_time_missing_or_invalid")
    levels = {}
    # Official API names Bid/Ask are reversed; inspect Sell/Buy levels explicitly.
    for side in ("Sell", "Buy"):
        valid = 0
        for index in range(1, 11):
            level = board.get(f"{side}{index}")
            if (
                isinstance(level, dict)
                and _number(level.get("Price"), positive=True)
                and _number(level.get("Qty"))
            ):
                valid += 1
            else:
                issues.append(f"{side.lower()}_{index}_missing_or_invalid")
        levels[side.lower()] = valid
    return {
        "issues": issues,
        "valid_levels": levels,
        "current_price_time": quote_time,
        "source_updated_at": None,  # No single update time for the whole board.
        "published_at": None,
        "freshness_verified": False,
        "market_fill_verified": False,
    }


def check_connection(password, root, *, transport=None):
    """Keep tokens in memory; preserve the board response under a new run directory."""
    run = root / uuid4().hex
    run.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "source": "mufg_esmart_kabustation_api",
        "symbol": "7203@1",
        "status": "failed",
        "api_authenticated": False,
        "board_received": False,
        "real_execution_enabled": False,
        "order_requests": 0,
        "requests": [],
    }
    token = None
    try:
        if not isinstance(password, str) or not re.fullmatch(r"[A-Za-z0-9]{6,16}", password):
            raise ProbeError("api_password_format_invalid")
        with httpx.Client(
            transport=ReadOnlyTransport(transport),
            timeout=5,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            status, body = _read(client, "POST", BASE + "/token", json={"APIPassword": password})
            report["requests"].append({"operation": "token", "http_status": status})
            if status != 200:
                raise ProbeError(f"token_http_{status}")
            auth = _object(body)
            token = auth.get("Token")
            if (
                type(auth.get("ResultCode")) is not int
                or auth["ResultCode"] != 0
                or not isinstance(token, str)
                or not re.fullmatch(r"[\x21-\x7e]{1,4096}", token)
            ):
                raise ProbeError("invalid_token_response")
            report["api_authenticated"] = True
            # Never persist the token response, request headers, or password.
            auth = body = None
            report["read_started_at"] = datetime.now(UTC).isoformat()
            status, body = _read(client, "GET", BASE + BOARD_PATH, headers={"X-API-KEY": token})
            completed = datetime.now(UTC)
            report["read_completed_at"] = completed.isoformat()
            report["requests"].append({"operation": "board", "http_status": status})
            if _contains_secret(body, (password, token)):
                raise ProbeError("sensitive_response_not_saved")
            atomic_write(run / "board.raw.json", body)
            report["raw_sha256"] = digest(body)
            if status != 200:
                raise ProbeError(f"board_http_{status}")
            board = _object(body)
            report["board_received"] = True
            report["quality"] = inspect_board(board, completed)
            if "unexpected_symbol_or_exchange" in report["quality"]["issues"]:
                raise ProbeError("unexpected_symbol_or_exchange")
            report["status"] = (
                "connected_with_attention" if report["quality"]["issues"] else "connected"
            )
    except ProbeError as exc:
        report["reason"] = str(exc)
    except httpx.HTTPError:
        report["reason"] = "network_request_failed"
    finally:
        password = token = None
        report["finished_at"] = datetime.now(UTC).isoformat()
        atomic_write(run / "report.json", canonical(report).encode())
    return run, report


def local_password_prompt():
    """The user types into a masked local dialog; no credential is written to disk."""
    import tkinter as tk
    from tkinter import simpledialog

    window = tk.Tk()
    window.withdraw()
    window.attributes("-topmost", True)
    try:
        return simpledialog.askstring(
            "kabuステーション 本番API 接続確認（取得専用・発注なし）",
            "アプリのAPIシステム設定の【本番用】APIパスワードを入力してください。\n"
            "検証用のパスワードではありません。本番APIでも発注は行いません。\n"
            "認証と7203の株価・板の取得を1回だけ行います。\n"
            "パスワードは保存・表示しません。ログイン用／取引用パスワードではありません。",
            show="*",
            parent=window,
        )
    finally:
        window.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/mufg-esmart-checks"))
    parser.add_argument(
        "--prompt", action="store_true", help="Ask the user locally; no CLI password"
    )
    args = parser.parse_args(argv)
    if not args.prompt:
        print('{"status":"not_started","reason":"local_prompt_required"}')
        return 2
    try:
        password = local_password_prompt()
        if password is None:
            print('{"status":"cancelled","real_execution_enabled":false}')
            return 2
        run, report = check_connection(password, args.root)
        password = None
        # The report contains only safe metadata, not raw price data or credentials.
        print(json.dumps({"report_path": str(run / "report.json"), **report}), flush=True)
        return 0 if report["status"].startswith("connected") else 2
    except Exception:
        print('{"status":"failed","reason":"local_probe_failed","real_execution_enabled":false}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
