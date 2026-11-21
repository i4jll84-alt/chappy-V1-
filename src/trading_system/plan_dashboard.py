"""Read-only A/B paper account dashboard and a background launcher for catch-up."""

import json
import subprocess
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from trading_system.plan_forward import STATUS_LABELS
from trading_system.plan_holdings import HoldingsReader, positive
from trading_system.plan_performance import PROFIT_COLOURS, performance, profit_display, profit_tag
from trading_system.plan_schedule import EVENING_CHECK

JST = ZoneInfo("Asia/Tokyo")
PREOPEN_CHECK = time(7, 40)
STEP_LABELS = {
    "daily": "日足・銘柄情報",
    "jsf": "日証金",
    "earnings": "決算予定",
    "margin": "信用残高",
    "flow": "投資部門別売買",
    "legacy_close": "既存口座の記帳",
    "short_public": "空売り公表情報",
    "review": "日次レビュー",
    "plans": "ペーパー口座更新",
    "a_preopen": "Aプランの寄付前判定（初回）",
    "native_evening": "Bプランの引け判定",
    "native_positions": "Bプランの建玉・受渡",
    "next_panel": "次の営業日の研究用データ",
    "intraday_panel": "次の営業日の研究用データ",
}
REASONS = {
    "required_file_missing": "必要な保存ファイルが見つかりません",
    "incomplete_daily_jobs": "日足保存の未完了項目があります",
    "incomplete_recovery_calendar": "取引所カレンダーが不足しています",
    "wrong_daily_receipt_date": "日足の記録日が対象日と一致しません",
    "earnings_receipt_unverified": "決算予定の取得記録を確認できません",
    "margin_not_yet_available": "当日分の信用残高が未配信です",
    "flow_refresh_unverified": "投資部門別の更新が未確認です",
    "financial_archive_coverage_unknown": "過去の決算データの収録範囲が未確認です",
    "panel_deadline_passed": "研究用データの作成期限を過ぎました",
    "public_digest_mismatch": "公表資料の照合に失敗しました",
    "legacy_close_missing": "引け記帳の記録がありません",
    "legacy_close_unconfirmed": "引け記帳が未確認です",
    "legacy_review_unconfirmed": "日次レビューが未確認です",
    "broker_state_already_open": "他の処理が使用中です",
}
STEP_STATES = {
    "running": "実行中",
    "ready": "確認済み",
    "recovered": "今回処理済み",
    "ok": "完了",
    "attention": "要確認",
    "error": "失敗",
    "stopped": "停止",
    "empty": "該当データなし",
    "skipped": "対象外",
    "missed": "期限経過",
    "pending": "待機中",
    "busy": "他で処理中",
}


def read_report(path):
    try:
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value, None
    except FileNotFoundError:
        return {}, "記録待ち"
    except (OSError, ValueError):
        return {}, "読込エラー"


def clock_label(value):
    if not value:
        return "—"
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is None:
            return "時刻不明"
        return stamp.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    except (TypeError, ValueError):
        return "時刻不明"


def step_result(step):
    dates = step.get("dates")
    if not isinstance(dates, dict):
        dates = {}
    failed = [
        d
        for d, result in sorted(dates.items())
        if isinstance(result, dict) and result.get("status") in ("error", "missed", "stopped")
    ]
    reason = step.get("reason") or ""
    detail = REASONS.get(reason, reason)
    if failed:
        detail = f"{failed[0]}　{detail}" + (
            f"（ほか{len(failed) - 1}日）" if len(failed) > 1 else ""
        )
    return detail


def yen(value):
    if value is None:
        return "—"
    try:
        amount = Decimal(str(value))
        return f"{amount:,.0f}円" if amount.is_finite() else "—"
    except InvalidOperation:
        return "—"


def share_price(value):
    number = positive(value)
    if number is None:
        return "未確認"
    return f"{number:,.3f}".rstrip("0").rstrip(".")


def account_line(plan):
    state = plan.get("status") or "未確認"
    decision = plan.get("decision_status") or plan.get("decision_state")
    if state == "waiting_close" and decision in ("missed_preopen", "stopped", "stopped_preopen"):
        state = decision
    label = STATUS_LABELS.get(state, state)
    if state == "waiting_close" and decision == "ready":
        label = "寄付候補確定" if plan.get("picks") else "判定済・候補0"
    elif state == "ready" and plan.get("candidates") == 0:
        label = "判定済・候補0"
    count = plan.get("positions")
    if isinstance(count, (dict, list)):
        count = len(count)
    nav = plan.get("nav")
    net, percent = performance(nav, plan.get("capital"))
    mark = plan.get("last_mark") or ("開始残高" if nav is not None else "未評価")
    return (
        label,
        yen(nav),
        profit_display(net, percent),
        "—" if count is None else str(count),
        mark,
        plan.get("next_entry") or "—",
    )


def receipt_time(receipt, field):
    try:
        value = datetime.fromisoformat(receipt[field])
        if value.utcoffset() is not None:
            return value.astimezone(JST)
    except (KeyError, ValueError, TypeError):
        pass
    return None


def saved_evening_work(project, now, recovery):
    today = now.date().isoformat()
    target = recovery.get("target")
    if now.time() >= time(16, 30) and now.weekday() < 5:
        if not (target and target < today < (recovery.get("next_session") or "")):
            target = today
    try:
        target = date.fromisoformat(target).isoformat()
    except (ValueError, TypeError):
        return "夜間処理の対象取引日を確認中"
    data = project / "data"
    daily, daily_issue = read_report(
        data / "jquants/daily-snapshots" / target / "last_collection.json"
    )
    evening, evening_issue = read_report(
        data / "plan-forward/recovery/evening" / (target + ".json")
    )
    daily_stamp = receipt_time(daily, "completed_at")
    daily_text = daily_issue or "要確認"
    if daily.get("date") == target and daily.get("state") == "complete":
        daily_text = "保存済み" + (f" {daily_stamp:%H:%M}" if daily_stamp else "")
    status = evening.get("status")
    evening_text = evening_issue or STEP_STATES.get(status, status or "要確認")
    end = receipt_time(evening, "completed_at") or receipt_time(evening, "checked_at")
    if status == "running":
        evening_text = "開始記録あり"
    elif end:
        evening_text += f" {end:%H:%M}"
    return f"{target} の記録　　日足 {daily_text}　　引け処理 {evening_text}"


def morning_check(root, day):
    """Use the trade date itself as evidence; yesterday's success cannot cover today."""
    report, issue = read_report(root / "morning-check" / (day + ".json"))
    if issue == "記録待ち":
        latest, latest_issue = read_report(root / "morning-check/latest.json")
        if latest.get("target") == day:
            report, issue = latest, None
        elif latest_issue == "読込エラー":
            issue = latest_issue
    if issue:
        return {}, ("まだ判定記録がありません" if issue == "記録待ち" else issue)
    if report.get("target") != day or not isinstance(report.get("plans"), dict):
        return {}, "対象日・記録形式を確認してください"
    return report, None


def check_time(receipt, field, day):
    stamp = receipt_time(receipt, field)
    if stamp is None:
        return "—"
    return stamp.strftime("%H:%M:%S" if stamp.date().isoformat() == day else "%m/%d %H:%M:%S")


def morning_text(value):
    # Use the current wording for saved receipts without rewriting their evidence.
    return (value or "").replace("以前の確定判定を使用", "確定判定を使用")


def morning_evidence(report, day, issue, expired=False):
    if issue:
        return f"対象寄付日 {day}　　{issue}"
    title = morning_text(report.get("label")) or "判定状態を確認してください"
    if expired and report.get("state") == "pending":
        title = "期限経過・寄付前の両プラン確定を確認できません"
    return (
        f"{title}　　対象寄付日 {day}　　選定日 {report.get('signal_date') or '—'}\n"
        f"朝の開始 {check_time(report, 'started_at', day)}　　"
        f"全口座確定 {check_time(report, 'decision_at', day)}　　"
        f"期限 {check_time(report, 'deadline', day)} JST"
    )


def plan_evidence(report, key, expired=False):
    result = report.get("plans", {}).get(key)
    if not isinstance(result, dict):
        return ""
    day = report["target"]
    stamp = check_time(result, "decision_at", day)
    label = morning_text(result.get("label")) or "判定待ち"
    if expired and result.get("state") == "pending":
        label = "期限経過・確定記録なし"
    line = f"{key}　{label}　　確定 {stamp}"
    if result.get("candidate_count") is not None:
        line += f"　　候補 {result['candidate_count']}銘柄"
    if result.get("order_count") is not None:
        line += f"　　予定 {result['order_count']}件"
    return line


def needs_preopen_retry(plans, receipt, day):
    """Retry only while an active strategy still needs a decision for this open."""
    pending = {
        "waiting_inputs",
        "waiting_close_inputs",
        "waiting_calendar",
        "blocked",
        "awaiting_native_decision",
        "missing_native_decision",
        "error",
    }
    for p in plans.values():
        target = p.get("next_entry")
        if p.get("start", "") >= day or (target and target > day):
            continue
        state = p.get("status")
        if state in ("disabled", "stopped") or (
            target == day
            and p.get("decision_status") in ("stopped", "missed_preopen", "stopped_preopen")
        ):
            continue
        has_target = target == day or receipt.get("next_session") == day
        if has_target and state in pending:
            return True
        if target == day and state == "waiting_signal":
            return True
        if (
            receipt.get("next_session") == day
            and target
            and target < day
            and (p.get("last_decision") or "") < (receipt.get("target") or "")
        ):
            # The latest summary may still describe yesterday's healthy order.
            return True
    return False


def spawn_catchup(project, action="--catch-up"):
    executable = project / ".venv/Scripts/python.exe"
    return subprocess.Popen(
        [str(executable), "-B", "-X", "utf8", "-m", "trading_system.plan_forward", action],
        cwd=project,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class PlanDashboard:
    def __init__(self, notebook, project, lock_probe):
        import tkinter as tk
        from tkinter import ttk

        self.project = project
        self.root = project / "data/plan-forward"
        self.lock_probe = lock_probe
        self.child = None
        self.child_result = ""
        self.gate_day = None
        self.evening_day = None
        self.deadline_day = None
        self.last_attempt = None
        self.last_values = None
        self.plans = {}
        self.steps = {}
        self.morning = {}
        self.notebook = notebook

        self.frame = tab = ttk.Frame(notebook, padding=(20, 12))
        notebook.add(tab, text="各プランの稼働状況")
        heading = ttk.Frame(tab)
        heading.pack(fill="x")
        ttk.Label(heading, text="フォワードテスト", style="Title.TLabel").pack(side="left")
        self.checked = tk.StringVar(master=tab)
        ttk.Label(tab, textvariable=self.checked).pack(anchor="w", pady=(0, 10))
        self.schedule_label = tk.StringVar(master=tab)
        ttk.Label(tab, textvariable=self.schedule_label).pack(anchor="w", pady=(0, 6))
        self.saved_label = tk.StringVar(master=tab)
        ttk.Label(tab, textvariable=self.saved_label).pack(anchor="w", pady=(0, 5))

        morning_box = ttk.LabelFrame(tab, text="朝の起動だけで寄付に間に合ったか", padding=(9, 4))
        morning_box.pack(fill="x", pady=(0, 7))
        self.morning_label = tk.StringVar(master=tab)
        ttk.Label(
            morning_box, textvariable=self.morning_label, justify="left", wraplength=740
        ).pack(anchor="w")
        self.plan_proof = tk.StringVar(master=tab)
        ttk.Label(morning_box, textvariable=self.plan_proof, justify="left", wraplength=740).pack(
            anchor="w"
        )

        self.accounts = self.table(
            tab,
            [
                ("plan", "口座", 70),
                ("state", "判定・処理状態", 170),
                ("nav", "純資産", 100),
                ("net", "評価損益・率", 205),
                ("hold", "保有数", 54),
                ("mark", "最終評価日", 98),
                ("entry", "対象寄付日", 100),
            ],
            height=2,
        )
        self.profit_colours(self.accounts)
        self.accounts.column("net", minwidth=175)
        self.account_field = "plan"
        for key in self.accounts["columns"]:
            self.accounts.heading(key, command=lambda field=key: self.select_account_field(field))
        self.accounts.bind("<<TreeviewSelect>>", lambda _e: self.show_account())
        ttk.Label(
            tab, text="口座の行を選び、項目名（純資産・保有数など）を押すと詳細を表示します。"
        ).pack(anchor="w", pady=(4, 0))
        self.detail = tk.StringVar(master=tab)
        self.detail_box = ttk.LabelFrame(tab, text="口座の詳細", padding=(8, 4))
        self.detail_box.pack(fill="x", pady=(4, 10))
        self.detail_text = tk.Text(
            self.detail_box,
            height=4,
            wrap="word",
            state="disabled",
            relief="flat",
            cursor="arrow",
            takefocus=True,
        )
        detail_scroll = ttk.Scrollbar(
            self.detail_box, orient="vertical", command=self.detail_text.yview
        )
        self.detail_text.configure(yscrollcommand=detail_scroll.set)
        self.detail_text.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")
        for tag, colour in PROFIT_COLOURS.items():
            self.detail_text.tag_configure(tag, foreground=colour)
        self.detail.trace_add("write", lambda *_: self.render_account_detail())

        rec = ttk.LabelFrame(tab, text="夜間処理の確認・復旧", padding=(10, 6))
        rec.pack(fill="both", expand=True)
        self.recovery_label = tk.StringVar(master=tab)
        ttk.Label(rec, textvariable=self.recovery_label, justify="left", wraplength=805).pack(
            anchor="w", pady=(0, 6)
        )
        self.recovery = self.table(
            rec,
            [
                ("stage", "処理", 150),
                ("date", "対象日", 100),
                ("state", "状態", 120),
                ("reason", "結果・理由", 310),
            ],
            height=6,
            expand=True,
        )
        self.step_detail = tk.StringVar(master=tab)
        ttk.Label(rec, textvariable=self.step_detail, wraplength=805, justify="left").pack(
            anchor="w", pady=(3, 0)
        )
        self.recovery.bind("<<TreeviewSelect>>", lambda _e: self.show_step())
        self.add_holdings(notebook)
        self.timer = tab.after_idle(self.on_startup)
        tab.bind("<Destroy>", self.on_destroy, add=True)

    @staticmethod
    def table(parent, fields, height, expand=False):
        from tkinter import ttk

        wrapper = ttk.Frame(parent)
        wrapper.pack(fill="both" if expand else "x", expand=expand)
        tree = ttk.Treeview(
            wrapper,
            columns=[f[0] for f in fields],
            show="headings",
            height=height,
            selectmode="browse",
        )
        scroll = ttk.Scrollbar(wrapper, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        for key, name, width in fields:
            tree.heading(key, text=name)
            tree.column(
                key,
                width=width,
                minwidth=min(width, 50),
                anchor="e" if key in {"nav", "net", "hold"} else "w",
            )
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    @staticmethod
    def profit_colours(tree):
        from tkinter import ttk

        style = ttk.Style(tree)
        # Keep gain/loss colours readable even when the row is selected.
        style.map(
            "Profit.Treeview",
            foreground=[("disabled", "#777777")],
            background=[("selected", "#e7edf6")],
        )
        tree.configure(style="Profit.Treeview")
        for tag, colour in PROFIT_COLOURS.items():
            tree.tag_configure(tag, foreground=colour)

    def add_holdings(self, notebook):
        import tkinter as tk
        from tkinter import ttk

        tab = ttk.Frame(notebook, padding=(20, 12))
        notebook.add(tab, text="保有銘柄")
        ttk.Label(tab, text="フォワードテストの保有銘柄", style="Title.TLabel").pack(anchor="w")
        self.holdings_status = tk.StringVar(master=tab)
        ttk.Label(tab, textvariable=self.holdings_status, wraplength=800, justify="left").pack(
            anchor="w", pady=(8, 10)
        )
        self.holdings = self.table(
            tab,
            [
                ("plan", "口座", 50),
                ("code", "コード", 65),
                ("name", "銘柄名", 260),
                ("shares", "株数", 65),
                ("entry_price", "取得価格（円）", 110),
                ("current_price", "現在価格（円）", 110),
                ("net", "評価損益・率", 205),
                ("price_date", "終値の日付", 105),
                ("entry_date", "取得日", 105),
                ("note", "備考", 220),
            ],
            height=12,
            expand=True,
        )
        self.profit_colours(self.holdings)
        for key in self.holdings["columns"]:
            self.holdings.column(key, stretch=False)
        for key in ("shares", "entry_price", "current_price", "net"):
            self.holdings.column(key, anchor="e")
        scroll = ttk.Scrollbar(tab, orient="horizontal", command=self.holdings.xview)
        scroll.pack(fill="x")
        self.holdings.configure(xscrollcommand=scroll.set)
        ttk.Label(
            tab,
            text="現在価格は保存済み日足の最新終値です。終値の日付を確認してください。\n"
            "評価損益・率は購入時費用込みの取得総額が基準です。売却前のため売却費用は含みません。\n"
            "プラスは赤、マイナスは青、ゼロ・未確認は通常色です。分割・併合は備考に表示します。",
            wraplength=800,
            justify="left",
        ).pack(anchor="w", pady=(10, 0))
        self.holdings_reader = HoldingsReader(self.project)
        self.last_holdings = None

    def refresh_holdings(self, plans, now):
        data = self.holdings_reader.read(plans, now)
        if data == self.last_holdings:
            return
        self.last_holdings = data
        self.holdings_status.set(data["status"] + ("\n" + data["issue"] if data["issue"] else ""))
        if self.holdings.get_children():
            self.holdings.delete(*self.holdings.get_children())
        for row in data["rows"]:
            code = row["code"]
            display_code = code[:-1] if len(code) == 5 and code.endswith("0") else code
            self.holdings.insert(
                "",
                "end",
                tags=(profit_tag(row.get("net")),),
                values=(
                    row["plan"],
                    display_code,
                    row["name"],
                    share_price(row["shares"]),
                    share_price(row["entry_price"]),
                    share_price(row["current_price"]),
                    profit_display(row.get("net"), row.get("return_pct")),
                    row["price_date"] or "未確認",
                    row["entry_date"] or "未確認",
                    row["note"],
                ),
            )

    def on_destroy(self, event):
        if event.widget == self.frame and self.timer:
            self.frame.after_cancel(self.timer)
            self.timer = None

    def on_startup(self):
        # Opening the tool starts the configured paper programs at any hour.
        # The worker/dispatch lock checks are shared with timed runs.
        if (self.project / "config/forward-plans.json").is_file():
            self.start()
        self.tick()

    def tick(self, opening=False):
        self.refresh()
        now = datetime.now(JST)
        self.saved_label.set(saved_evening_work(self.project, now, self.last_values[2]))
        self.schedule_label.set("07:40 寄付前確認　　平日22:00 日足保存・情報取得・引け処理")
        self.check_morning(now, opening)
        self.check_deadline(now)
        self.check_evening(now)
        self.timer = self.frame.after(2000, self.tick)

    def check_morning(self, now, opening=False):
        today = now.date()
        gate = PREOPEN_CHECK
        after_gate = now.time() >= gate
        if today.weekday() >= 5 or (not after_gate and not opening):
            return
        # Allow a window left open overnight to resume during the session after sleep.
        if opening and now.hour >= 12:
            self.gate_day = today
            return
        if now.time() >= time(15, 30) or not (self.project / "config/forward-plans.json").is_file():
            return
        recovery = self.last_values[2]
        if (
            (recovery.get("target") or "")
            < today.isoformat()
            < (recovery.get("next_session") or "")
        ):
            return
        if self.last_values[4]:
            return  # A worker already owns the recovery; check its receipt on the next tick.
        retry = (
            after_gate
            and now.time() < time(9)
            and needs_preopen_retry(self.plans, recovery, today.isoformat())
        )
        if self.gate_day == today and not retry:
            return
        if retry:
            attempts = [
                v
                for v in (self.last_attempt, receipt_time(recovery, "checked_at"))
                if v and v.date() == today and v.time() >= gate
            ]
            if attempts:
                at = max(attempts) + timedelta(minutes=5)
                if at > now:
                    if at.time() < time(9):
                        self.schedule_label.set(
                            f"寄付前の未確定分を {at:%H:%M} に再確認　　"
                            "平日22:00 日足保存・情報取得・引け処理"
                        )
                    return
        if recovery.get("status") == "ok":
            began = receipt_time(recovery, "started_at")
            if (
                began
                and began.date() == today
                and not retry
                and (not after_gate or began.time() >= gate)
            ):
                if after_gate:
                    self.gate_day = today
                return
        self.start()

    def check_deadline(self, now):
        """Record the first post-open outcome for an actual outstanding morning check."""
        day = now.date()
        if (
            self.deadline_day == day
            or self.last_values[4]
            or self.morning.get("target") != day.isoformat()
            or self.morning.get("state") != "pending"
            or not time(9) <= now.time() < time(15, 30)
        ):
            return
        self.deadline_day = day
        self.start()

    def check_evening(self, now):
        day = now.date()
        if now.time() < EVENING_CHECK or day.weekday() >= 5 or self.evening_day == day:
            return
        if self.last_values[4] or not (self.project / "config/forward-plans.json").is_file():
            return
        rec = self.last_values[2]
        if (rec.get("target") or "") < day.isoformat() < (rec.get("next_session") or ""):
            return
        path = self.root / "recovery/evening" / (day.isoformat() + ".json")
        saved, _ = read_report(path)
        if saved.get("status") == "ok":
            self.evening_day = day
            return
        self.start("--evening-check")

    def busy(self):
        if self.child is not None and self.child.poll() is None:
            return True
        try:
            return self.lock_probe(self.root / "recovery/dispatch/writer.lock")
        except OSError:
            return False

    def start(self, action="--catch-up"):
        if self.busy():
            self.refresh()
            return
        now = datetime.now(JST)
        if action == "--evening-check":
            self.evening_day = now.date()
        else:
            self.last_attempt = now
            if now.time() >= PREOPEN_CHECK:
                self.gate_day = now.date()
        try:
            self.child = spawn_catchup(self.project, action)
            self.child_result = ""
        except OSError:
            self.child = None
            self.child_result = (
                "復旧処理を起動できません。Python環境を確認し、ツールを開き直してください。"
            )
        self.refresh()

    def refresh(self):
        if self.child is not None and (code := self.child.poll()) is not None:
            self.child_result = (
                "" if code == 0 else "未完了の処理があります。記録を確認してください。"
            )
            self.child = None
        running = self.busy()
        summary, summary_issue = read_report(self.root / "summary.json")
        rec, rec_issue = read_report(self.root / "recovery/latest.json")
        now = datetime.now(JST)
        day = now.date().isoformat()
        expired = now.time() >= time(9)
        proof, proof_issue = morning_check(self.root, day)
        values = (
            summary,
            summary_issue,
            rec,
            rec_issue,
            running,
            self.child_result,
            day,
            proof,
            proof_issue,
            expired,
        )
        plans = summary.get("plans", {})
        if not isinstance(plans, dict) or not all(isinstance(p, dict) for p in plans.values()):
            plans = {}
        self.refresh_holdings(plans, now)
        if values == self.last_values:
            self.show_account()  # A newly saved price may update holdings without changing NAV.
            return
        self.last_values = values
        self.morning = proof
        self.morning_label.set(morning_evidence(proof, day, proof_issue, expired))
        self.plan_proof.set(
            "\n".join(line for k in ("A", "B", "C") if (line := plan_evidence(proof, k, expired)))
        )
        self.checked.set(
            "ペーパー口座　　最終確認 " + (summary_issue or clock_label(summary.get("checked_at")))
        )
        self.plans = summary.get("plans", {})
        if not isinstance(self.plans, dict) or not all(
            isinstance(p, dict) for p in self.plans.values()
        ):
            self.plans = {}
            self.checked.set("ペーパー口座の集計形式を確認してください。")
        for key in self.accounts.get_children():
            if key not in self.plans:
                self.accounts.delete(key)
        for key, p in self.plans.items():
            row = (key + "プラン", *account_line(p))
            net, _ = performance(p.get("nav"), p.get("capital"))
            tags = (profit_tag(net),)
            if self.accounts.exists(key):
                self.accounts.item(key, values=row, tags=tags)
            else:
                self.accounts.insert("", "end", iid=key, values=row, tags=tags)
        if not self.accounts.selection() and self.accounts.get_children():
            self.accounts.selection_set(self.accounts.get_children()[0])
        self.show_account()
        target = rec.get("target") or "—"
        when = clock_label(rec.get("checked_at") or rec.get("started_at"))
        state = rec.get("status", "")
        if running:
            title = "実行中"
        elif state == "running":
            title = "前回の処理が途中で終了・要再確認"
        else:
            title = rec_issue or STEP_STATES.get(state, state or "未確認")
        detail = rec.get("reason") or self.child_result
        self.recovery_label.set(
            f"{title}　　対象取引日 {target}　　最終確認 {when}" + (f"\n{detail}" if detail else "")
        )
        self.steps = rec.get("steps", {})
        if not isinstance(self.steps, dict) or not all(
            isinstance(s, dict) for s in self.steps.values()
        ):
            self.steps = {}
            self.recovery_label.set("復旧記録の形式を確認してください。")
        for key in self.recovery.get_children():
            if key not in self.steps:
                self.recovery.delete(key)
        for key, step in self.steps.items():
            stage = step.get("status", "")
            state_label = STEP_STATES.get(stage, stage or "未確認")
            if stage == "running" and not running:
                state_label = "前回中断"
            fields = (
                STEP_LABELS.get(key, key),
                step.get("date") or "—",
                state_label,
                step_result(step),
            )
            if self.recovery.exists(key):
                self.recovery.item(key, values=fields)
            else:
                self.recovery.insert("", "end", iid=key, values=fields)
        self.show_step()

    def select_account_field(self, field):
        self.account_field = field
        self.show_account()
        self.detail_text.yview_moveto(0)

    def render_account_detail(self):
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", self.detail.get())
        for index, line in enumerate(self.detail.get().splitlines(), 1):
            if line.startswith("評価損益　"):
                value = line.removeprefix("評価損益　")
                tag = (
                    "gain"
                    if value.startswith("+")
                    else "loss"
                    if value.startswith("-")
                    else "neutral"
                )
                self.detail_text.tag_add(tag, f"{index}.0", f"{index}.end")
        self.detail_text.configure(state="disabled")

    def show_account(self):
        chosen = self.accounts.selection()
        if not chosen:
            self.detail_box.configure(text="口座の詳細")
            value = "口座の集計記録を待っています。"
        else:
            key = chosen[0]
            field = self.account_field
            self.detail_box.configure(text=f"{self.accounts.heading(field, 'text')} — {key}プラン")
            value = self.account_detail(key, field)
        if self.detail.get() != value:
            self.detail.set(value)

    def account_detail(self, key, field):
        plan = self.plans.get(key, {})
        line = account_line(plan)
        issue = plan.get("issue") or plan.get("reason")
        count = plan.get("candidates")
        if plan.get("decision_status") == "ready":
            count = len(plan.get("picks", []))
        evidence = self.morning.get("plans", {}).get(key)
        source = morning_text(evidence.get("source_note")) if isinstance(evidence, dict) else None
        summary = (
            f"{key}　{plan.get('name', '')}　　現金 {yen(plan.get('cash'))}　　"
            f"最終判定 {plan.get('last_decision') or '未判定'}"
            + (f"　　候補 {count}銘柄" if count is not None else "")
            + (f"　　予定 {plan['orders']}件" if plan.get("orders") is not None else "")
            + (f"\n{issue}" if issue else "")
            + (f"\n朝の判定根拠　{source}" if source else "")
        )
        if field == "plan":
            return summary
        if field == "state":
            return f"判定・処理状態　{line[0]}\n対象寄付日　{line[5]}\n" + summary
        if field == "nav":
            return (
                f"純資産　{line[1]}\n最終評価日　{line[4]}\n"
                f"口座集計の現金　{yen(plan.get('cash'))}\n"
                f"開始資金　{yen(plan.get('capital'))}"
            )
        if field == "net":
            return (
                f"評価損益　{line[2]}\n"
                f"純資産 {line[1]} − 開始資金 {yen(plan.get('capital'))}\n"
                f"最終評価日　{line[4]}\n損益率は開始資金が基準です。"
                "口座に記帳済みの費用を反映しています。"
            )
        if field == "mark":
            return (
                f"最終評価日　{line[4]}\n純資産　{line[1]}\n評価損益　{line[2]}\n"
                "保存済みの口座評価の日付です。保有銘柄の終値の日付とは別に確認してください。"
            )
        if field == "entry":
            rows = [
                f"対象寄付日　{line[5]}",
                f"対象選定日　{plan.get('next_signal') or plan.get('last_decision') or '未確認'}",
                f"確定時刻　{clock_label(plan.get('decision_recorded_at'))}",
                summary,
            ]
            if plan.get("picks"):
                rows.append("保存済みの候補銘柄")
                rows.extend(f"{p['code']}　{p.get('name', '')}" for p in plan["picks"])
            return "\n".join(rows)
        data = self.last_holdings or {}
        held = [row for row in data.get("rows", []) if row["plan"] == key]
        rows = [f"保有数　{line[3]}銘柄"]
        if not held:
            rows.append(
                "現在の保有銘柄はありません。" if line[3] == "0" else "保有明細を確認できません。"
            )
        for row in held:
            code = row["code"]
            code = code[:-1] if len(code) == 5 and code.endswith("0") else code
            prices = [
                "未確認" if positive(row[k]) is None else share_price(row[k]) + "円"
                for k in ("entry_price", "current_price")
            ]
            rows.extend(
                [
                    f"{code}　{row['name']}　{share_price(row['shares'])}株",
                    f"取得価格 {prices[0]}　最新終値 {prices[1]}"
                    f"（{row['price_date'] or '日付未確認'}）",
                    f"評価損益　{profit_display(row.get('net'), row.get('return_pct'))}",
                ]
            )
            if row.get("note"):
                rows.append(row["note"])
        if held and data.get("issue"):
            rows.append(data["issue"])
        return "\n".join(rows)

    def show_step(self):
        keys = self.recovery.selection()
        reason = step_result(self.steps.get(keys[0], {})) if keys else None
        self.step_detail.set(reason or "")
