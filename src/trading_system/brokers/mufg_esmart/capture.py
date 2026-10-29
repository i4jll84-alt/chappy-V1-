"""Run the read-only MUFG feed until 16:00 JST; no scheduled or startup registration."""

import argparse
import json
from pathlib import Path

from trading_system.brokers.market_data import run
from trading_system.brokers.models import Account
from trading_system.brokers.mufg_esmart.market_data import Client, RecoveryProbe
from trading_system.brokers.registry import vendor_adapter


def password_prompt():
    import tkinter as tk
    from tkinter import simpledialog

    window = tk.Tk()
    window.withdraw()
    window.attributes("-topmost", True)
    try:
        return simpledialog.askstring(
            "三菱UFJ 取得専用・16時終了（実発注なし）",
            "kabuステーションの【本番用】APIパスワードを入力してください。\n"
            "7203の株価・板を5秒以上の間隔で取得し、16時に保存・照合後終了します。\n"
            "注文・訂正・取消は行いません。パスワードとトークンは保存しません。\n"
            "接続確認のため、この取得処理だけを一時中断し復帰を確認する場合があります。",
            show="*",
            parent=window,
        )
    finally:
        window.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/mufg-continuous"))
    parser.add_argument("--prompt", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--recover", action="store_true", help="Offline recovery; no API or prompt")
    parser.add_argument("--max-seconds", type=int)
    parser.add_argument(
        "--verify-reconnect",
        action="store_true",
        help="Inject two local transport failures after four board responses",
    )
    args = parser.parse_args(argv)
    if sum((args.prompt, args.stop, args.recover)) != 1:
        print('{"status":"not_started","reason":"choose_prompt_stop_or_recover"}')
        return 2
    if args.stop:
        args.root.mkdir(parents=True, exist_ok=True)
        (args.root / "STOP").touch(exist_ok=True)
        print('{"status":"stop_requested","real_execution_enabled":false}')
        return 0
    probe = RecoveryProbe() if args.verify_reconnect else None

    def factory():
        if args.recover:
            return None
        password = password_prompt()
        if password is None:
            return None
        client = Client(password, transport_factory=probe.transport if probe else None)
        password = None
        return vendor_adapter(
            Account(broker="mufg_esmart", account_id="data-only"),
            enabled=True,
            market_data_client=client,
        )

    try:
        result = run(args.root, factory, max_seconds=args.max_seconds, recovery_probe=probe)
        print(json.dumps(result), flush=True)
        return 0 if result["normal_exit"] else 2
    except Exception:
        print('{"status":"failed","reason":"local_capture_failed","real_execution_enabled":false}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
