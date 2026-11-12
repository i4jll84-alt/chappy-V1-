"""Run the read-only MUFG feed until 16:00 JST; no scheduled or startup registration."""

import argparse
import json
from pathlib import Path

from trading_system.brokers.market_data import run
from trading_system.brokers.models import Account
from trading_system.brokers.mufg_esmart.market_data import Client, RecoveryProbe
from trading_system.brokers.registry import vendor_adapter
from trading_system.tk_startup import create_window


def password_prompt(stop_reason, target="7203の株価・板", *, one_pass=False):
    """Wait for input on the Tk thread, waking every 200 ms to check the capture's stop rule."""
    import tkinter as tk
    from tkinter import ttk

    window = create_window(tk, stop_reason)
    if window is None:
        return None
    window.withdraw()
    try:
        window.title(
            "カブコム・市況を1回取得（実発注なし）"
            if one_pass
            else "三菱UFJ 取得専用・16時終了（実発注なし）"
        )
        window.attributes("-topmost", True)
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="kabuステーションの本番用APIパスワードを入力してください。\n"
            f"{target}を取得します。パスワードは保存しません。\n"
            + (
                "対象の銘柄を1巡して終了します。入力待ちは最長5分です。"
                if one_pass
                else "停止要求または日本時間16時でこの画面を閉じ、保存・照合します。"
            ),
        ).pack(anchor="w")
        entry = ttk.Entry(frame, show="*")
        entry.pack(fill="x", pady=8)
        action = tk.StringVar(window)
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e")
        ttk.Button(buttons, text="開始", command=lambda: action.set("submit")).pack(side="left")
        ttk.Button(buttons, text="取消", command=lambda: action.set("cancel")).pack(side="left")
        window.bind("<Return>", lambda _: action.set("submit"))
        window.bind("<Escape>", lambda _: action.set("cancel"))
        window.protocol("WM_DELETE_WINDOW", lambda: action.set("cancel"))
        if stop_reason():
            return None
        window.deiconify()
        entry.focus_set()
        while not stop_reason():
            timer = window.after(200, action.set, "poll")
            try:
                window.wait_variable(action)
            finally:
                window.after_cancel(timer)
            if action.get() == "cancel":
                return None
            if action.get() == "submit" and not stop_reason():
                return entry.get()
        return None
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

    def factory(stop_reason):
        if args.recover:
            return None
        password = password_prompt(stop_reason)
        if password is None or stop_reason():
            return None
        client = Client(password, transport_factory=probe.transport if probe else None)
        password = None
        return vendor_adapter(
            Account(broker="mufg_esmart", account_id="data-only"),
            enabled=True,
            market_data_client=client,
        )

    try:
        result = run(
            args.root, wait_for_adapter=factory, max_seconds=args.max_seconds, recovery_probe=probe
        )
        print(json.dumps(result), flush=True)
        return 0 if result["normal_exit"] else 2
    except Exception:
        print('{"status":"failed","reason":"local_capture_failed","real_execution_enabled":false}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
