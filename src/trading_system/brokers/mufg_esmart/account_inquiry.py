"""One-shot local cash, position and order inquiry with a minimal reconciliation record."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from trading_system.storage.store import atomic_write, canonical

from .connection_check import BASE, ProbeError, _number, _object, _read

ROUTES = ("/wallet/cash", "/positions?product=1", "/orders?product=1")


class InquiryTransport(httpx.BaseTransport):
    def __init__(self, inner=None):
        self.inner = inner if inner is not None else httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        allowed = {("GET", BASE + route) for route in ROUTES} | {("POST", BASE + "/token")}
        if (request.method, str(request.url)) not in allowed:
            raise ProbeError("request_not_allowed")
        return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


def query(password, *, transport=None):
    report = dict(
        started_at=datetime.now(UTC).isoformat(),
        status="incomplete",
        requests=[],
        real_orders_submitted=0,
    )
    try:
        with httpx.Client(
            transport=InquiryTransport(transport),
            timeout=5,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            status, body = _read(client, "POST", BASE + "/token", json={"APIPassword": password})
            report["requests"].append(["token", status])
            if status != 200:
                raise ProbeError("authentication_failed")
            auth = _object(body)
            token = auth.get("Token")
            if (
                auth.get("ResultCode") != 0
                or not isinstance(token, str)
                or not re.fullmatch(r"[!-~]{1,4096}", token)
            ):
                raise ProbeError("invalid_auth_response")
            for route in ROUTES:
                status, body = _read(client, "GET", BASE + route, headers={"X-API-KEY": token})
                report["requests"].append([route, status])
                if status != 200:
                    raise ProbeError("account_read_failed")
                data = json.loads(body)
                if route == ROUTES[0]:
                    wallet = data["StockAccountWallet"]
                    if not _number(wallet):
                        raise ProbeError("invalid_cash_wallet")
                    report["cash_buying_power_yen"] = wallet
                elif not isinstance(data, list):
                    raise ProbeError("invalid_account_list")
                elif route == ROUTES[1]:
                    if any(not _number(x["LeavesQty"]) for x in data):
                        raise ProbeError("invalid_position_qty")
                    report["held_symbols"] = sorted(
                        {x["Symbol"] for x in data if x["LeavesQty"] > 0}
                    )
                else:
                    if any(
                        type(x["State"]) is not int or x["State"] not in range(1, 6) for x in data
                    ):
                        raise ProbeError("unknown_order_state")
                    remaining = [x for x in data if x["State"] != 5]
                    report["open_order_count"] = len(remaining)
                    report["open_order_symbols"] = sorted({x["Symbol"] for x in remaining})
        report["status"] = "complete"
    except ProbeError as exc:
        report["reason"] = str(exc)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        report["reason"] = "connection_or_response_invalid"
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def main():
    import argparse
    import tkinter as tk
    from tkinter import simpledialog

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dialog = tk.Tk()
    dialog.withdraw()
    dialog.attributes("-topmost", True)
    try:
        password = simpledialog.askstring(
            "三菱UFJ eスマート証券・口座照会",
            "kabuステーションの本番APIパスワードを入力してください。\n現物買付可能額・保有・注文状況を1回照会します。\nパスワードは保存しません。",
            show="*",
            parent=dialog,
        )
    finally:
        dialog.destroy()
    if not password:
        return 2
    report = query(password)
    path = args.output / (uuid4().hex + ".json")
    atomic_write(path, canonical(report).encode())
    print(report["status"], path, flush=True)
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
