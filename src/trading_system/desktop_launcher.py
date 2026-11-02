"""One local control panel for the existing Rakuten and MUFG read-only collectors."""

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from time import time_ns
from uuid import uuid4
from zoneinfo import ZoneInfo

from trading_system.rss_stop import publish_stop, stop_ticket

BROKERS = ("rakuten", "mufg_esmart")
CAPTURES = (*BROKERS, "panel", "kabu_panel")
NAMES = {
    "rakuten": "楽天証券",
    "mufg_esmart": "カブコム / 三菱UFJ eスマート証券",
    "panel": "楽天31銘柄研究",
    "kabu_panel": "カブコム31銘柄研究",
}
ROOTS = {
    "rakuten": "rss-continuous",
    "mufg_esmart": "mufg-continuous",
    "panel": "rss-panel",
    "kabu_panel": "mufg-panel",
}
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


def rakuten_progress(project, requested_ns):
    """Only display a report from the current launch, never yesterday's status."""
    try:
        data = json.loads((collector_root(project, "rakuten") / "status.json").read_bytes())
        started = datetime.fromisoformat(data["started_at"])
        if started.tzinfo is None or started.timestamp() < requested_ns / 1_000_000_000:
            return None
        phase = data["phase"]
        if phase == "restoring":
            done, total = data.get("recovery_runs_checked"), data.get("recovery_runs_total")
            if type(done) is int and type(total) is int and 0 <= done <= total:
                return f"過去データを復元中（{done:,} / {total:,} 組）"
            return "過去データを復元中"
        if phase == "saving_parquet":
            return "データを書き出しています"
        if phase == "verifying_saved_data":
            return "保存したデータを照合しています"
        if phase == "complete" and data.get("normal_exit") is True:
            return "保存・照合が完了しました。"
        if data.get("status") == "failed":
            return "処理が異常終了しました。取得画面と保存記録を確認してください。"
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        pass
    return None


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
    if broker in ("rakuten", "panel", "kabu_panel"):
        publish_stop(root)
        return
    root.mkdir(parents=True, exist_ok=True)
    (root / "STOP").write_text("stop requested from desktop launcher", encoding="utf-8")


def rss_cutoff(requested_ns):
    """Unix seconds at 15:59 JST on the date the operator requested Start."""
    local = datetime.fromtimestamp(requested_ns // 1_000_000_000, ZoneInfo("Asia/Tokyo"))
    return int(local.replace(hour=15, minute=59, second=0, microsecond=0).timestamp())


def launch_command(project, broker, *, resume_stop="none", cutoff=None):
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
            "-RecoveryCutoffUnix",
            str(cutoff if cutoff is not None else rss_cutoff(time_ns())),
            "-ResumeStop",
            resume_stop,
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
        self.rakuten_requested_ns = None
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
        ttk.Button(actions, text="すべての取得を停止", command=self.stop_all).pack(side="left")
        ttk.Button(actions, text="設定", command=self.configure).pack(side="right")
        study = ttk.Frame(frame)
        study.pack(fill="x", pady=(6, 0))
        ttk.Button(study, text="楽天31銘柄の研究取得", command=self.start_panel).pack(
            side="left", padx=(0, 10)
        )
        ttk.Button(
            study, text="カブコム31銘柄の研究取得", command=lambda: self.start_panel("kabu_panel")
        ).pack(side="left")
        status_frame = ttk.LabelFrame(frame, text="取得状態", padding=12)
        status_frame.pack(fill="x", pady=15)
        self.status = {broker: tk.StringVar() for broker in CAPTURES}
        self.panel_requested_ns = 0
        for broker in CAPTURES:
            ttk.Label(status_frame, textvariable=self.status[broker], wraplength=710).pack(
                anchor="w", pady=5
            )
        ttk.Label(
            frame,
            text=(
                "通常の開始は7203の接続確認。研究ボタンは30銘柄＋トヨタの分足と気配を記録します。\n"
                "場中の継続取得は16時で終了。引け後の研究ボタンは市況を一度読み取ります。\n"
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

    def open_workbook(self, cutoff=None):
        from tkinter import messagebox

        now = time_ns()
        if cutoff is not None and now // 1_000_000_000 >= cutoff:
            self.message.set("通常取得は終了時刻のため、保存済みデータを照合します。")
            return None
        try:
            os.startfile(self.workbook)
            return True
        except FileNotFoundError:
            messagebox.showerror("Excel", "楽天用のRSS接続確認.xlsxが見つかりません。")
            return False
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
            if name == "rakuten_app" and path.suffix.lower() == ".exe":
                # MARKET SPEED II resolves Config/Localize from its working directory.
                executable = path.absolute()
                os.startfile(executable, cwd=executable.parent)
            else:
                os.startfile(path)
        except OSError:
            messagebox.showerror("アプリ", "アプリを開けません。設定の保存先を確認してください。")

    def busy(self, broker):
        child = self.children.get(broker)
        return (child is not None and child.poll() is None) or collector_active(
            self.project, broker
        )

    def start(self):
        requested = time_ns()
        from tkinter import messagebox

        try:
            chosen = self.selected()
            cutoff = rss_cutoff(requested) if "rakuten" in chosen else None
            resume_stop = "none"
            if cutoff is not None and requested // 1_000_000_000 < cutoff:
                root = collector_root(self.project, "rakuten")
                marker = root / "STOP"
                if not marker.exists():
                    marker = root / "STOP.resuming"
                resume_stop = stop_ticket(marker, requested)
            # Check all selections before launching any, so partial starts are explicit.
            for broker in chosen:
                if self.busy(broker):
                    messagebox.showinfo(
                        "起動済み", NAMES[broker] + "は起動中です。二重起動しません。"
                    )
                    return
            # A slow workbook path lookup can itself cross into offline recovery.
            if (
                cutoff is not None
                and time_ns() // 1_000_000_000 < cutoff
                and not self.workbook.is_file()
                and time_ns() // 1_000_000_000 < cutoff
            ):
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
            # A modal confirmation above may have crossed the cutoff or midnight.
            if (
                cutoff is not None
                and time_ns() // 1_000_000_000 < cutoff
                and self.open_workbook(cutoff) is False
            ):
                return
            for broker in chosen:
                self.stopping.discard(broker)
                self.children[broker] = subprocess.Popen(
                    launch_command(self.project, broker, resume_stop=resume_stop, cutoff=cutoff),
                    cwd=self.project,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
                if broker == "rakuten":
                    self.rakuten_requested_ns = requested
            self.message.set("開始しました。取得の終了時刻以降は、保存済みデータの照合を行います。")
        except (OSError, ValueError):
            messagebox.showerror(
                "起動",
                "起動できませんでした。既に起動した取得は状態欄と停止ボタンで確認してください。",
            )

    def start_panel(self, broker="panel"):
        from tkinter import messagebox

        requested = time_ns()
        late = requested // 1_000_000_000 >= rss_cutoff(requested)
        try:
            if self.busy(broker) or (broker == "kabu_panel" and self.busy("mufg_esmart")):
                self.message.set("同じ取得元が起動中です。既存の取得の終了を確認してください。")
                return
            root = collector_root(self.project, broker)
            marker = root / "STOP"
            resume = stop_ticket(marker if marker.exists() else root / "STOP.resuming", requested)
            self.children[broker] = subprocess.Popen(
                [
                    str(self.project / ".venv/Scripts/python.exe"),
                    "-B",
                    "-X",
                    "utf8",
                    "-m",
                    "trading_system.research_panel"
                    if broker == "panel"
                    else "trading_system.kabu_study",
                    "--requested",
                    str(requested),
                    "--resume",
                    resume,
                    *(["--snapshot-now"] if late else []),
                ],
                cwd=self.project,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if broker == "panel":
                self.panel_requested_ns = requested
            self.stopping.discard(broker)
            self.message.set(
                NAMES[broker]
                + ("：引け後の記録を取得して終了します。" if late else "：研究取得を開始しました。")
            )
        except (OSError, ValueError):
            messagebox.showerror(
                "研究取得", "起動できません。研究用ブックと保存先を確認してください。"
            )

    def stop_all(self):
        from tkinter import messagebox

        failed = []
        for broker in CAPTURES:
            try:
                request_stop(self.project, broker)
            except OSError:
                failed.append(NAMES[broker])
            else:
                self.stopping.add(broker)
        if failed:
            messagebox.showerror(
                "停止",
                "停止要求を記録できませんでした。取得画面から停止してください。\n"
                + "、".join(failed),
            )
        else:
            self.message.set(
                "停止を要求しました。入力画面は自動で閉じます。保存完了までお待ちください。"
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
        for broker in CAPTURES:
            try:
                child = self.children.get(broker)
                if self.busy(broker):
                    text = (
                        "停止処理中 / 保存完了を待っています"
                        if broker in self.stopping
                        else "起動中（接続確認・保存処理を含む）"
                    )
                    if broker == "rakuten" and child is not None and child.poll() is None:
                        detail = rakuten_progress(self.project, self.rakuten_requested_ns)
                        if detail:
                            text = ("停止要求済み / " if broker in self.stopping else "") + detail
                    if broker == "panel" and broker not in self.stopping:
                        try:
                            data = json.loads(
                                (collector_root(self.project, broker) / "status.json").read_bytes()
                            )
                            if (
                                datetime.fromisoformat(data["updated_at"]).timestamp()
                                >= self.panel_requested_ns / 1e9
                            ):
                                if data["state"] == "read":
                                    text = (
                                        f"直近の有効気配 {data['books']}/31銘柄、"
                                        f"確認した分足 {data['valid_bars_seen']}本"
                                    )
                                elif data["state"] not in ("starting", "stopped"):
                                    text = "研究用Excel・RSSの応答待ち（自動再確認）"
                        except (OSError, ValueError, KeyError, TypeError):
                            pass
                    if broker == "kabu_panel" and broker not in self.stopping:
                        try:
                            data = json.loads(
                                (collector_root(self.project, broker) / "status.json").read_bytes()
                            )
                            if data.get("state") == "prompt":
                                text = "PCの専用画面でAPIパスワードを入力してください。"
                            elif data.get("state") == "recording":
                                text = f"板を巡回中（累計{data['board_reads']}回照会）"
                        except (ValueError, KeyError, OSError):
                            pass
                elif child is not None:
                    text = "終了。取得画面と保存記録で結果を確認してください。"
                    if child.returncode:
                        text = "異常終了。取得画面と保存記録を確認してください。"
                else:
                    text = "停止中。保存済みの過去の状態は現在の稼働を示しません。"
            except OSError:
                text = "状態を確認できません。取得画面を確認してください。"
            self.status[broker].set(NAMES[broker] + "：" + text)
        self.window.after(2000, self.refresh)

    def close(self):
        from tkinter import messagebox

        try:
            active = any(self.busy(b) for b in CAPTURES)
        except OSError:
            active = True
        if active:
            messagebox.showinfo(
                "終了前の確認",
                "取得または入力待ちが残っています。停止ボタンを押し、保存完了後に終了してください。",
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
