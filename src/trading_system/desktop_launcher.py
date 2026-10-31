"""One local control panel for the existing Rakuten and MUFG read-only collectors."""

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

BROKERS = ("rakuten", "mufg_esmart")
NAMES = {"rakuten": "楽天証券", "mufg_esmart": "カブコム / 三菱UFJ eスマート証券"}
ROOTS = {"rakuten": "rss-continuous", "mufg_esmart": "mufg-continuous"}
PROJECT = Path(__file__).resolve().parents[2]


def default_settings():
    local = Path(os.environ["LOCALAPPDATA"])
    return {
        "brokers": ["rakuten"],
        "rakuten_app": str(local / "MarketSpeed2/Bin/MarketSpeed2.exe"),
        "mufg_app": str(local / "kabuStation/KabuS.exe"),
    }


def validate_settings(value):
    if not isinstance(value, dict) or set(value) != {
        "brokers",
        "rakuten_app",
        "mufg_app",
    }:
        raise ValueError("設定ファイルの形式を確認してください。")
    brokers = value["brokers"]
    if (
        not isinstance(brokers, list)
        or not brokers
        or any(b not in BROKERS for b in brokers)
        or len(brokers) != len(set(brokers))
    ):
        raise ValueError("起動する証券会社を選択してください。")
    for name in ("rakuten_app", "mufg_app"):
        if not isinstance(value[name], str):
            raise ValueError("アプリの保存先を確認してください。")
        if value[name] and Path(value[name]).suffix.lower() not in {".lnk", ".exe"}:
            raise ValueError("アプリ本体かショートカットを指定してください。")
    return value.copy()


def save_settings(path, value):
    value = validate_settings(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def collector_root(project, broker):
    return project / "data" / ROOTS[broker]


def collector_active(project, broker):
    """Probe the OS lock; old status files alone are never evidence of a running process."""
    import msvcrt

    name = "collector.lock" if broker == "rakuten" else "writer.lock"
    lock = collector_root(project, broker) / name
    if not lock.exists():
        return False
    with lock.open("r+b") as handle:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    return False


def archive_mufg_stop(project):
    """Called only after the operator explicitly confirms resuming a stopped collector."""
    from trading_system.brokers.locking import single_writer

    root = collector_root(project, "mufg_esmart")
    with single_writer(root):
        stop = root / "STOP"
        if stop.exists():
            history = root / "stop-history"
            history.mkdir(exist_ok=True)
            stop.replace(history / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + uuid4().hex))


def request_stop(project, broker):
    root = collector_root(project, broker)
    root.mkdir(parents=True, exist_ok=True)
    (root / "STOP").write_text("stop requested from desktop launcher", encoding="utf-8")


def launch_command(project, broker):
    if broker == "rakuten":
        shell = Path(os.environ.get("SystemRoot", "C:/Windows")) / (
            "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        return [
            str(shell),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(project / "scripts" / "start_rss_continuous.ps1"),
            "-Action",
            "Start",
        ]
    if broker == "mufg_esmart":
        return [
            str(project / ".venv" / "Scripts" / "python.exe"),
            "-B",
            "-X",
            "utf8",
            "-m",
            "trading_system.brokers.mufg_esmart.capture",
            "--prompt",
            "--root",
            str(collector_root(project, broker)),
        ]
    raise ValueError("Unsupported broker")


class Launcher:
    def __init__(self, window, project=PROJECT):
        import tkinter as tk
        from tkinter import messagebox, ttk

        self.window, self.project = window, project
        self.children = {}
        self.stopping = set()
        self.settings_path = project / "data" / "desktop-launcher" / "settings.json"
        self.workbook = Path(os.environ["USERPROFILE"]) / "Documents/RakutenRSS/RSS接続確認.xlsx"
        self.settings = default_settings()
        if self.settings_path.exists():
            try:
                self.settings = validate_settings(json.loads(self.settings_path.read_text("utf-8")))
            except (ValueError, OSError):
                messagebox.showwarning(
                    "設定", "保存済み設定を読み込めません。初期設定を表示します。"
                )
        window.title("個別株ツール — 楽天証券・カブコム 共通起動")
        window.geometry("860x720")
        window.minsize(820, 700)
        window.option_add("*Font", ("Yu Gothic UI", 11))
        style = ttk.Style(window)
        style.theme_use("clam")
        style.configure("TButton", padding=(12, 8))
        style.configure("Title.TLabel", font=("Yu Gothic UI", 20, "bold"))
        frame = ttk.Frame(window, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="個別株データ取得", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="楽天証券とカブコムを、ひとつの画面から操作します。").pack(
            anchor="w", pady=(4, 15)
        )
        selection = ttk.LabelFrame(frame, text="起動する証券会社", padding=12)
        selection.pack(fill="x")
        current = self.settings["brokers"]
        self.mode = tk.StringVar(value="both" if len(current) == 2 else current[0])
        for value, label in (
            ("rakuten", "楽天証券"),
            ("mufg_esmart", "カブコム"),
            ("both", "両方"),
        ):
            ttk.Radiobutton(selection, text=label, value=value, variable=self.mode).pack(
                side="left", padx=(0, 30)
            )
        preparation = ttk.Frame(frame)
        preparation.pack(fill="x", pady=12)
        ttk.Button(preparation, text="楽天のExcelを開く", command=self.open_workbook).pack(
            side="left", padx=(0, 7)
        )
        ttk.Button(
            preparation,
            text="MARKET SPEED II",
            command=lambda: self.open_app("rakuten_app"),
        ).pack(side="left", padx=7)
        ttk.Button(
            preparation,
            text="kabuステーション",
            command=lambda: self.open_app("mufg_app"),
        ).pack(side="left", padx=7)
        ttk.Label(
            frame,
            text="楽天：ExcelのRSSを「接続中・発注不可」にして開始します。\nカブコム：APIパスワードは開始時に入力します。保存しません。",
            wraplength=740,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        ttk.Button(actions, text="選択した証券会社を開始", command=self.start).pack(
            side="left", padx=(0, 10)
        )
        ttk.Button(actions, text="両方の取得を停止", command=self.stop_all).pack(side="left")
        ttk.Button(actions, text="設定", command=self.configure).pack(side="right")
        status_frame = ttk.LabelFrame(frame, text="取得状態", padding=12)
        status_frame.pack(fill="x", pady=15)
        self.status = {broker: tk.StringVar() for broker in BROKERS}
        for broker in BROKERS:
            ttk.Label(status_frame, textvariable=self.status[broker], wraplength=710).pack(
                anchor="w", pady=5
            )
        ttk.Label(
            frame,
            text=(
                "対象：7203 トヨタ自動車（接続確認用）\n"
                "取得は日本時間16時に終了します。16時以降の開始は保存済みデータの後処理です。\n"
                "現在は取得・ペーパートレード用です。実発注は無効です。"
            ),
            wraplength=740,
            justify="left",
        ).pack(anchor="w")
        self.message = tk.StringVar(
            value="証券アプリのログイン後に、開始する対象を選んでください。"
        )
        ttk.Label(frame, textvariable=self.message, foreground="#7A233B", wraplength=740).pack(
            anchor="w", pady=(12, 0)
        )
        window.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def selected(self):
        return list(BROKERS) if self.mode.get() == "both" else [self.mode.get()]

    def open_workbook(self):
        from tkinter import messagebox

        if not self.workbook.is_file():
            messagebox.showerror("Excel", "楽天用のRSS接続確認.xlsxが見つかりません。")
            return False
        try:
            os.startfile(self.workbook)
            return True
        except OSError:
            messagebox.showerror(
                "Excel", "Excelを開けません。Excelのインストールを確認してください。"
            )
            return False

    def open_app(self, name):
        from tkinter import messagebox

        path = Path(self.settings[name])
        if not path.is_file():
            messagebox.showinfo(
                "アプリの設定",
                "設定から証券アプリ本体かショートカットを指定してください。",
            )
            return
        try:
            os.startfile(path)
        except OSError:
            messagebox.showerror("アプリ", "アプリを開けません。設定の保存先を確認してください。")

    def busy(self, broker):
        child = self.children.get(broker)
        return (child is not None and child.poll() is None) or collector_active(
            self.project, broker
        )

    def start(self):
        from tkinter import messagebox

        try:
            chosen = self.selected()
            # Check all selections before launching any, so partial starts are explicit.
            for broker in chosen:
                if self.busy(broker):
                    messagebox.showinfo(
                        "起動済み", NAMES[broker] + "は起動中です。二重起動しません。"
                    )
                    return
            if "rakuten" in chosen and not self.workbook.is_file():
                messagebox.showerror("Excel", "楽天用Excelシートが見つかりません。")
                return
            if (
                "mufg_esmart" in chosen
                and (collector_root(self.project, "mufg_esmart") / "STOP").exists()
            ):
                if not messagebox.askyesno(
                    "取得の再開",
                    "カブコムの停止要求が残っています。\n停止済みの取得を再開しますか？停止要求は履歴へ保存します。",
                ):
                    return
                archive_mufg_stop(self.project)
            self.settings["brokers"] = chosen
            save_settings(self.settings_path, self.settings)
            if "rakuten" in chosen and not self.open_workbook():
                return
            for broker in chosen:
                self.stopping.discard(broker)
                self.children[broker] = subprocess.Popen(
                    launch_command(self.project, broker),
                    cwd=self.project,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
            self.message.set(
                "起動しました。楽天は自動でデータを検査します。カブコムは入力画面で操作を続けてください。"
            )
        except (OSError, ValueError):
            messagebox.showerror(
                "起動",
                "起動できませんでした。既に起動した取得は状態欄と停止ボタンで確認してください。",
            )

    def stop_all(self):
        from tkinter import messagebox

        try:
            for broker in BROKERS:
                request_stop(self.project, broker)
                self.stopping.add(broker)
            self.message.set(
                "停止を要求しました。保存完了までお待ちください。入力画面がある場合は取り消してください。"
            )
        except OSError:
            messagebox.showerror(
                "停止", "停止要求を書き込めません。各取得画面から停止してください。"
            )

    def configure(self):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        dialog = tk.Toplevel(self.window)
        dialog.title("共通起動の設定")
        dialog.geometry("760x350")
        dialog.transient(self.window)
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        fields = {}
        for name, label in (
            ("rakuten_app", "MARKET SPEED II"),
            ("mufg_app", "kabuステーション"),
        ):
            ttk.Label(frame, text=label).pack(anchor="w")
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=(3, 12))
            fields[name] = tk.StringVar(value=self.settings[name])
            ttk.Entry(row, textvariable=fields[name]).pack(side="left", fill="x", expand=True)

            def choose(variable=fields[name]):
                value = filedialog.askopenfilename(
                    parent=dialog, filetypes=[("アプリ・ショートカット", "*.exe *.lnk")]
                )
                if value:
                    variable.set(value)

            ttk.Button(row, text="参照", command=choose).pack(side="right", padx=(8, 0))
        ttk.Label(
            frame,
            text="起動対象の選択も一緒に保存します。APIパスワードやログイン情報は保存しません。",
            wraplength=700,
        ).pack(anchor="w", pady=6)
        ttk.Label(
            frame,
            text=(
                "楽天のExcel：Documents / RakutenRSS / RSS接続確認.xlsx\n"
                "取得先：ローカルのプロジェクト内 data。実発注の設定は変更しません。"
            ),
            wraplength=700,
        ).pack(anchor="w", pady=6)

        def save():
            candidate = {
                "brokers": self.selected(),
                **{k: v.get() for k, v in fields.items()},
            }
            try:
                save_settings(self.settings_path, candidate)
                self.settings = candidate
                dialog.destroy()
                self.message.set("共通起動の設定を保存しました。")
            except (OSError, ValueError):
                messagebox.showerror(
                    "設定",
                    "保存できません。アプリ本体かショートカットを指定してください。",
                    parent=dialog,
                )

        ttk.Button(frame, text="設定を保存", command=save).pack(anchor="e", pady=8)

    def refresh(self):
        for broker in BROKERS:
            try:
                child = self.children.get(broker)
                if self.busy(broker):
                    text = (
                        "停止処理中 / 入力画面は取り消してください"
                        if broker in self.stopping
                        else "起動中（接続確認・保存処理を含む）"
                    )
                elif child is not None:
                    text = "終了。取得画面と保存記録で結果を確認してください。"
                else:
                    text = "停止中。保存済みの過去の状態は現在の稼働を示しません。"
            except OSError:
                text = "状態を確認できません。取得画面を確認してください。"
            self.status[broker].set(NAMES[broker] + "：" + text)
        self.window.after(2000, self.refresh)

    def close(self):
        from tkinter import messagebox

        try:
            active = any(self.busy(b) for b in BROKERS)
        except OSError:
            active = True
        if active:
            messagebox.showinfo(
                "終了前の確認",
                "取得または入力待ちが残っています。停止ボタンで停止し、入力画面を閉じてから終了してください。",
            )
        else:
            self.window.destroy()


def main():
    import tkinter as tk
    from tkinter import messagebox

    from trading_system.brokers.locking import single_writer

    window = tk.Tk()
    window.withdraw()
    try:
        with single_writer(PROJECT / "data" / "desktop-launcher"):
            Launcher(window)
            window.deiconify()
            window.mainloop()
    except ValueError:
        messagebox.showinfo(
            "共通起動",
            "起動画面はすでに開いています。タスクバーを確認してください。",
            parent=window,
        )
    except Exception:
        messagebox.showerror(
            "共通起動",
            "起動画面を開けませんでした。ローカルの設定とPython環境を確認してください。",
            parent=window,
        )
    finally:
        try:
            window.destroy()
        except tk.TclError:
            pass


if __name__ == "__main__":
    main()
