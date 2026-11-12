"""One local control panel for the existing Rakuten and MUFG read-only collectors."""

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time
from pathlib import Path
from time import time_ns
from uuid import uuid4
from zoneinfo import ZoneInfo

from trading_system.research_status import active_status, terminal_guidance
from trading_system.rss_stop import publish_stop, stop_ticket
from trading_system.tk_startup import create_window

BROKERS = ("rakuten", "mufg_esmart")
RESEARCH = {"rakuten": "panel", "mufg_esmart": "kabu_panel"}
CAPTURES = (*BROKERS, *RESEARCH.values())
NAMES = {
    "rakuten": "楽天 接続確認",
    "mufg_esmart": "カブコム 接続確認",
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
        "brokers": list(BROKERS),
        "rakuten_app": str(local / "MarketSpeed2/Bin/MarketSpeed2.exe"),
        "mufg_app": str(local / "kabuStation/KabuS.exe"),
    }


def open_vendor(name, path):
    if name == "rakuten_app" and path.suffix.lower() == ".exe":
        executable = path.absolute()
        os.startfile(executable, cwd=executable.parent)
    else:
        os.startfile(path)


def ensure_broker_app(project, source, path):
    """Run outside Tk. Only open an app if the current Windows session lacks it."""
    name = "rakuten_app" if source == "rakuten" else "mufg_app"
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not path.is_file():
        raise FileNotFoundError
    result = subprocess.run(
        [
            str(shell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(project / "scripts/broker_app_running.ps1"),
            "-AppPath",
            str(path.absolute()),
            "-Broker",
            source,
        ],
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 3:
        open_vendor(name, path)
    elif result.returncode != 0:
        raise OSError("broker_app_probe_failed")


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


def market_day_settings(project):
    from trading_system.settings import Settings

    settings = Settings.load(project / "config/stage1.toml")
    settings.data_root = project / settings.data_root
    return settings


def save_market_day(project, day):
    from trading_system.research.swing_daily import collect

    return collect(day, market_day_settings(project))


def panel_status(report):
    panel = report.get("next_panel", {})
    if panel.get("state") == "ready":
        return f" ／ {panel['target_session']} 用候補30銘柄＋トヨタを保存"
    if panel.get("state") == "failed":
        return " ／ 翌営業日候補の作成失敗。「当日分を保存」で再試行してください。"
    return ""


def rakuten_progress(project, requested_ns):
    """Only display a report from the current launch, never yesterday's status."""
    try:
        data = json.loads((collector_root(project, "rakuten") / "status.json").read_bytes())
        started = datetime.fromisoformat(data["started_at"])
        if started.tzinfo is None or started.timestamp() < requested_ns / 1_000_000_000:
            return None
        phase = data["phase"]
        if phase == "restoring":
            done, total = (
                data.get("recovery_runs_checked"),
                data.get("recovery_runs_total"),
            )
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


def panel_bar_dates(data):
    if "current_day_chart_symbols" not in data:
        return ""
    text = f"、当日分足 {data['current_day_chart_symbols']}/31銘柄"
    if data.get("latest_chart_at"):
        latest = datetime.fromisoformat(data["latest_chart_at"]).astimezone(ZoneInfo("Asia/Tokyo"))
        text += f"（最新 {latest:%m/%d %H:%M}）"
    if "streaming_charts" in data:
        text += f"、RSS配信中 {data['streaming_charts']}/31"
    if data.get("connection_required_charts"):
        text += "。MARKET SPEED IIのログインを確認し、Excelが「未接続」なら押してください。"
    return text


def research_result(project, requested_ns=0, broker="panel"):
    """A terminal result is a past capture, never evidence that a collector is active."""
    from trading_system.research_panel import FAILURE_MESSAGES

    try:
        data = json.loads((collector_root(project, broker) / "status.json").read_bytes())
        at = datetime.fromisoformat(data["updated_at"])
        if at.tzinfo is None or at.timestamp() < requested_ns / 1e9:
            return None
        reason = data.get("reason")
        stamp = at.astimezone(ZoneInfo("Asia/Tokyo"))
        if data["state"] not in {"stopped", "not_started", "failed", "needs_review"}:
            return None
        guidance = terminal_guidance(data["state"], reason)
        if broker == "kabu_panel" or data["state"] == "not_started":
            detail = guidance or "停止。再開するには研究取得ボタンを押してください。"
            if reason == "one_pass_complete":
                detail += f" 有効な板 {data.get('symbols_with_book', 0)}/31銘柄。"
            return f"前回 {stamp:%m/%d %H:%M}：{detail}"
        if data["state"] == "failed":
            detail = "異常終了：" + FAILURE_MESSAGES.get(
                reason, "Excelと保存先を確認してください。"
            )
        elif data["state"] == "stopped" and reason == "one_pass_complete":
            detail = f"一括取得・保存完了（分足 {data.get('chart_symbols', 0)}/31銘柄）"
        elif data["state"] == "stopped" and reason == "one_pass_recent_only":
            detail = "直近分足を保存。当日分は全銘柄で揃っていません"
        elif data["state"] == "needs_review":
            detail = f"取得が未完了（分足 {data.get('chart_symbols', 0)}/31銘柄）"
            if data.get("streaming_charts") == 31:
                detail += "。当日分足が揃っていません"
            elif not data.get("connection_required_charts"):
                detail += "。研究用Excel・RSSの接続と応答を確認してください"
        elif data["state"] == "stopped":
            detail = "停止・保存完了"
        else:
            return None
        if data["state"] in {"stopped", "needs_review"}:
            detail += panel_bar_dates(data)
        if reason == "manual_stop":
            detail += "。再開するには研究取得ボタンを押してください。"
        elif reason == "daytime_1600":
            detail += "。16時の定時終了です。"
        return f"前回 {stamp:%m/%d %H:%M}：{detail}"
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None


def collector_active(project, broker):
    name = "collector.lock" if broker == "rakuten" else "writer.lock"
    return lock_active(collector_root(project, broker) / name)


def lock_active(lock):
    """Probe the OS lock; old status files alone are never evidence of a running process."""
    import msvcrt

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
        self.vendor_executor = ThreadPoolExecutor(max_workers=len(BROKERS))
        self.vendor_tasks = {}
        self.start_notes = {}
        self.requested = {}
        self.daily_executor = ThreadPoolExecutor(max_workers=1)
        self.daily_task = None
        self.daily_root = None
        self.daily_seen = None
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
        frame = ttk.Frame(window, padding=(24, 16))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="個別株データ取得", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text="「両方」を選んで研究取得を押すと、両社のアプリと取得が起動します。",
        ).pack(anchor="w", pady=(4, 8))
        selection = ttk.LabelFrame(frame, text="対象の証券会社", padding=12)
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
        preparation.pack(fill="x", pady=8)
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
        ttk.Button(
            preparation, text="楽天の接続確認用Excelを開く", command=self.open_workbook
        ).pack(side="left", padx=7)
        ttk.Label(
            frame,
            text="楽天：ログイン後、ExcelのRSSが「未接続」なら接続してください。\nカブコム：APIパスワードは開始時に入力します。保存しません。",
            wraplength=740,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        ttk.Button(
            actions,
            text="選択した証券会社の研究取得（31銘柄）",
            command=self.start_research,
        ).pack(side="left", padx=(0, 10))
        ttk.Button(actions, text="すべての取得を停止", command=self.stop_all).pack(side="left")
        ttk.Button(actions, text="設定", command=self.configure).pack(side="right")
        other_actions = ttk.Frame(frame)
        other_actions.pack(fill="x", pady=(8, 0))
        evening = ttk.LabelFrame(other_actions, text="夜の作業（16:30以降）", padding=(8, 4))
        evening.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ttk.Button(evening, text="当日分を保存", command=self.start_daily_save).pack(side="left")
        ttk.Label(evening, text="証券アプリ・Excelは不要です。").pack(side="left", padx=6)
        diagnostics = ttk.LabelFrame(other_actions, text="データが届かないとき", padding=(8, 4))
        diagnostics.pack(side="right", fill="y")
        ttk.Button(diagnostics, text="接続を確認（トヨタ1銘柄）", command=self.start).pack(
            side="left", padx=(0, 10)
        )
        status_frame = ttk.LabelFrame(frame, text="稼働状態", padding=12)
        status_frame.pack(fill="x", pady=10)
        self.status = {broker: tk.StringVar() for broker in CAPTURES}
        for broker in (*RESEARCH.values(), *BROKERS):
            ttk.Label(status_frame, textvariable=self.status[broker], wraplength=710).pack(
                anchor="w", pady=2
            )
        daily_row = ttk.Frame(status_frame)
        daily_row.pack(anchor="w", pady=2)
        ttk.Label(daily_row, text="夜の当日分保存：").pack(side="left")
        self.daily_status = tk.StringVar(value="待機中")
        ttk.Label(daily_row, textvariable=self.daily_status, wraplength=540).pack(side="left")
        ttk.Label(
            frame,
            text=(
                "研究取得では楽天の分足・気配、カブコムの板を記録します。楽天の研究用Excelは自動で開きます。\n"
                "場中の継続取得は16時で終了。引け後の研究ボタンは市況を一度読み取ります。\n"
                "現在は取得・ペーパートレード用です。実発注は無効です。"
            ),
            wraplength=740,
            justify="left",
        ).pack(anchor="w")
        self.message = tk.StringVar(
            value="ログインや入力が必要な場合は、稼働状態の各社の案内に従ってください。"
        )
        ttk.Label(frame, textvariable=self.message, foreground="#7A233B", wraplength=740).pack(
            anchor="w", pady=(12, 0)
        )
        window.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def selected(self):
        return list(BROKERS) if self.mode.get() == "both" else [self.mode.get()]

    def start_daily_save(self):
        if self.daily_task is not None:
            self.daily_status.set("保存中です。完了までお待ちください。")
            return
        now = datetime.fromtimestamp(time_ns() // 1_000_000_000, ZoneInfo("Asia/Tokyo"))
        if now.time() < time(16, 30):
            self.daily_status.set("当日分は16:30以降に保存できます。")
            return
        try:
            self.daily_task = self.daily_executor.submit(save_market_day, self.project, now.date())
        except RuntimeError:
            self.daily_status.set("保存を開始できません。画面を開き直してください。")
            return
        self.daily_status.set(f"{now:%m/%d} の日足・決算などを保存中です。")

    def refresh_daily_save(self):
        if self.daily_task is None:
            self.read_daily_state()
            return
        if not self.daily_task.done():
            return
        try:
            report = self.daily_task.result()
            if report["state"] == "closed_day":
                message = f"{report['date']} は休場日です。"
            elif report["state"] == "complete":
                message = (
                    f"{report['date']} 保存完了（日足 {report['daily_rows']:,} 銘柄）"
                    + panel_status(report)
                )
            else:
                message = "保存を確認できません。時間をおいて再実行してください。"
        except Exception as exc:
            # Only display known messages. Provider errors may contain request details.
            reasons = {
                "market_data_key_missing": "J-QuantsのAPIキー設定を確認してください。",
                "broker_state_already_open": "別の保存処理が実行中です。完了をお待ちください。",
                "daily_not_yet_available": "日足が未配信です。時間をおいて再実行してください。",
            }
            message = reasons.get(str(exc), "保存に失敗しました。通信と設定を確認してください。")
        self.daily_task = None
        self.daily_status.set(message)

    def read_daily_state(self):
        today = datetime.fromtimestamp(time_ns() // 1_000_000_000, ZoneInfo("Asia/Tokyo")).date()
        try:
            if self.daily_root is None:
                self.daily_root = market_day_settings(self.project).data_root / "daily-snapshots"
            busy = lock_active(self.daily_root / "writer.lock")
            path = self.daily_root / today.isoformat() / "last_collection.json"
            try:
                modified = path.stat().st_mtime_ns
            except FileNotFoundError:
                modified = None
            observation = (today, busy, modified)
            if observation == self.daily_seen:
                return
            if busy:
                message = "保存中（別の画面から実行）"
            else:
                message = "待機中"
                if modified is not None:
                    saved = json.loads(path.read_bytes())
                    if saved["state"] != "complete" or saved["date"] != today.isoformat():
                        raise ValueError("unexpected_daily_report")
                    message = (
                        f"停止中 ／ 本日の保存記録あり（日足 {saved['daily_rows']:,} 銘柄）"
                        + panel_status(saved)
                    )
            self.daily_seen = observation
        except (OSError, ValueError, KeyError, TypeError):
            self.daily_seen = None
            message = "稼働状態を確認できません。設定と保存先を確認してください。"
        self.daily_status.set(message)

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
            open_vendor(name, path)
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
                if broker == "mufg_esmart" and self.busy("kabu_panel"):
                    messagebox.showinfo(
                        "接続確認",
                        "カブコムの研究取得中です。接続確認は取得終了後に行えます。",
                    )
                    return
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
            self.message.set(
                "トヨタ1銘柄の接続確認を開始しました。取得終了時刻以降は保存記録を確認します。"
            )
        except (OSError, ValueError):
            messagebox.showerror(
                "起動",
                "起動できませんでした。既に起動した取得は状態欄と停止ボタンで確認してください。",
            )

    def start_research(self):
        from tkinter import messagebox

        requested = time_ns()
        late = requested // 1_000_000_000 >= rss_cutoff(requested)
        chosen = self.selected()
        settings = {**self.settings, "brokers": chosen}
        try:
            save_settings(self.settings_path, settings)
        except (OSError, ValueError):
            messagebox.showerror(
                "研究取得",
                "証券会社の選択を保存できません。設定と保存先を確認してください。",
            )
            return
        self.settings = settings
        for source in chosen:
            broker = RESEARCH[source]
            if broker in self.vendor_tasks:
                continue
            self.start_notes.pop(broker, None)
            try:
                if self.busy(broker) and broker in self.stopping:
                    continue
                if source == "mufg_esmart" and not self.busy(broker) and self.busy(source):
                    self.start_notes[broker] = (
                        "接続確認中。終了後に研究取得ボタンを押してください。"
                    )
                    continue
                self.stopping.discard(broker)
                if datetime.fromtimestamp(requested / 1e9, ZoneInfo("Asia/Tokyo")).hour < 6:
                    self.start_notes[broker] = (
                        "開始時間外。6:00以降に研究取得ボタンを押してください。"
                    )
                    continue
                key = "rakuten_app" if source == "rakuten" else "mufg_app"
                task = self.vendor_executor.submit(
                    ensure_broker_app, self.project, source, Path(settings[key])
                )
                self.vendor_tasks[broker] = (task, requested, late)
            except (OSError, RuntimeError):
                self.start_notes[broker] = (
                    "起動を確認できません。設定を確認して再度開始してください。"
                )
        self.message.set("選択した証券会社の状態と必要な操作は「稼働状態」で確認できます。")
        self.refresh_research_starts()

    def refresh_research_starts(self):
        for broker, (task, requested, late) in list(self.vendor_tasks.items()):
            if not task.done():
                continue
            del self.vendor_tasks[broker]
            if broker in self.stopping:
                continue
            try:
                task.result()
            except OSError as exc:
                if getattr(exc, "winerror", None) in {1260, 4551}:
                    self.start_notes[broker] = (
                        "Windowsがアプリの起動をブロックしました。"
                        "設定で正規の証券アプリを指定し、再度開始してください。"
                    )
                else:
                    self.start_notes[broker] = (
                        "証券アプリを開けません。設定のアプリの場所を確認して再度開始してください。"
                    )
                continue
            except subprocess.TimeoutExpired:
                self.start_notes[broker] = (
                    "証券アプリの起動確認が時間切れです。少し待って研究取得を押してください。"
                )
                continue
            try:
                if self.busy(broker):
                    continue
                if broker == "kabu_panel" and self.busy("mufg_esmart"):
                    self.start_notes[broker] = (
                        "接続確認中。終了後に研究取得ボタンを押してください。"
                    )
                    continue
                self.launch_research(broker, requested, late)
            except (OSError, ValueError):
                self.start_notes[broker] = (
                    "取得を起動できません。設定と保存先を確認して再度開始してください。"
                )

    def launch_research(self, broker, requested, late):
        root = collector_root(self.project, broker)
        marker = root / "STOP"
        resume = stop_ticket(marker if marker.exists() else root / "STOP.resuming", requested)
        module = (
            "trading_system.research_panel" if broker == "panel" else "trading_system.kabu_study"
        )
        self.children[broker] = subprocess.Popen(
            [
                str(self.project / ".venv/Scripts/python.exe"),
                "-B",
                "-X",
                "utf8",
                "-m",
                module,
                "--requested",
                str(requested),
                "--resume",
                resume,
                *(["--snapshot-now"] if late else []),
            ],
            cwd=self.project,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.requested[broker] = requested
        self.stopping.discard(broker)

    def stop_all(self):
        from tkinter import messagebox

        for task, _, _ in self.vendor_tasks.values():
            task.cancel()
        self.vendor_tasks.clear()
        self.start_notes.clear()
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
                "証券会社の取得に停止を要求しました。夜の当日分保存は完了までお待ちください。"
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
        self.refresh_research_starts()
        now = datetime.fromtimestamp(time_ns() / 1e9, UTC)
        for broker in CAPTURES:
            try:
                child = self.children.get(broker)
                # The kabu research collector also holds the connection check's writer lock.
                active = self.busy(broker)
                if broker == "mufg_esmart" and active and self.busy("kabu_panel"):
                    active = child is not None and child.poll() is None
                if active:
                    text = (
                        "停止処理中 / 保存完了を待っています"
                        if broker in self.stopping
                        else "起動中（準備・保存処理を含む）"
                    )
                    if broker == "rakuten" and child is not None and child.poll() is None:
                        detail = rakuten_progress(self.project, self.rakuten_requested_ns)
                        if detail:
                            text = ("停止要求済み / " if broker in self.stopping else "") + detail
                    if broker in RESEARCH.values() and broker not in self.stopping:
                        try:
                            data = json.loads(
                                (collector_root(self.project, broker) / "status.json").read_bytes()
                            )
                            updated = datetime.fromisoformat(data["updated_at"])
                            if (
                                updated.utcoffset() is not None
                                and updated.timestamp() >= self.requested.get(broker, 0) / 1e9
                            ):
                                text = active_status(broker, data, now)
                        except (
                            OSError,
                            ValueError,
                            KeyError,
                            TypeError,
                            OverflowError,
                        ):
                            text = "起動中・今回の状態報告を待っています。"
                elif child is not None:
                    text = "終了。取得画面と保存記録で結果を確認してください。"
                    if child.returncode:
                        text = "異常終了。取得画面と保存記録を確認してください。"
                else:
                    text = "停止中。保存済みの過去の状態は現在の稼働を示しません。"
                if broker in RESEARCH.values():
                    if not active:
                        detail = research_result(
                            self.project, self.requested.get(broker, 0), broker
                        )
                        if detail:
                            text = detail
                        elif child is None:
                            text = "停止中。開始するには研究取得ボタンを押してください。"
                    if broker in self.vendor_tasks:
                        if not active:
                            text = "証券アプリを起動確認中。お待ちください。"
                    elif broker in self.start_notes:
                        text = (text + " ／ " if active else "") + self.start_notes[broker]
            except OSError:
                text = "状態を確認できません。取得画面を確認してください。"
            self.status[broker].set(NAMES[broker] + "：" + text)
        self.refresh_daily_save()
        self.window.after(2000, self.refresh)

    def close(self):
        from tkinter import messagebox

        if self.daily_task is not None:
            messagebox.showinfo("当日分を保存", "夜の保存結果が表示されてから閉じてください。")
            return
        try:
            active = any(self.busy(b) for b in CAPTURES)
        except OSError:
            active = True
        if active or self.vendor_tasks:
            messagebox.showinfo(
                "終了前の確認",
                "取得または入力待ちが残っています。停止ボタンを押し、保存完了後に終了してください。",
            )
        else:
            self.vendor_executor.shutdown(wait=False)
            self.daily_executor.shutdown(wait=False)
            self.window.destroy()


def main():
    import tkinter as tk
    from tkinter import messagebox

    from trading_system.brokers.locking import single_writer

    window = create_window(tk)
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
