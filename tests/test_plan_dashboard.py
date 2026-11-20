import json
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from unittest.mock import Mock

import pytest

from trading_system import plan_dashboard as dashboard
from trading_system.tk_startup import create_window


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_reported_marks_and_frozen_empty_decision_differ_from_missing_inputs():
    base = dict(capital=3_000_000, cash=3_000_000, positions={}, next_entry="2026-11-20")
    assert dashboard.account_line(dict(base, status="waiting_inputs"))[:3] == (
        "株価・日証金データ待ち",
        "—",
        "—",
    )
    held = dict(base, nav=3_010_000, last_mark="2026-11-18")
    assert dashboard.account_line(dict(held, status="ready", candidates=0)) == (
        "判定済・候補0",
        "3,010,000円",
        "10,000円",
        "0",
        "2026-11-18",
        "2026-11-20",
    )
    assert (
        dashboard.account_line(
            dict(base, status="waiting_close", decision_status="ready", picks=[])
        )[0]
        == "判定済・候補0"
    )
    assert (
        dashboard.account_line(
            dict(base, status="waiting_close", decision_status="missed_preopen")
        )[0]
        == "寄付前判定が欠測"
    )
    assert (
        dashboard.account_line(dict(held, status="blocked", reason="当日入力不足"))[4]
        == "2026-11-18"
    )
    assert dashboard.clock_label("2026-11-18T23:30:00+00:00") == "2026-11-19 08:30 JST"


@pytest.mark.parametrize(
    "day,hh", [(19, 6), (19, 8), (19, 11), (19, 13), (19, 18), (19, 21), (21, 8), (23, 8)]
)
def test_dashboard_starts_in_background_and_shows_real_receipts(tmp_path, monkeypatch, day, hh):
    window = create_window(tk)
    window.withdraw()
    try:
        notebook = ttk.Notebook(window)
        notebook.pack()
        save(tmp_path / "config/forward-plans.json", {"plans": []})
        fake_clock = Mock()
        fake_clock.now.return_value = datetime(2026, 11, day, hh, tzinfo=dashboard.JST)
        monkeypatch.setattr(
            dashboard,
            "datetime",
            type("Clock", (datetime,), {"now": classmethod(lambda cls, tz: fake_clock.now(tz))}),
        )
        root = tmp_path / "data/plan-forward"
        save(
            root / "summary.json",
            dict(
                status="ok",
                checked_at="2026-11-19T00:05:00+09:00",
                plans={
                    "A": dict(
                        name="基準③",
                        status="waiting_inputs",
                        capital=3000000,
                        issue="日証金未着",
                        cash=3000000,
                        nav=3000000,
                        positions={},
                    ),
                    "B": dict(
                        name="A0-5",
                        status="ready",
                        capital=3000000,
                        cash=3000000,
                        nav=3000000,
                        positions=0,
                        candidates=0,
                        orders=0,
                        last_mark="2026-11-18",
                    ),
                },
            ),
        )
        save(
            root / "recovery/latest.json",
            dict(
                status="running",
                target="2026-11-18",
                checked_at="2026-11-19T08:05:00+09:00",
                steps={
                    "daily": dict(status="recovered", date="2026-11-18"),
                    "jsf": dict(status="running", date="2026-11-18"),
                },
            ),
        )
        child = Mock()
        child.poll.return_value = None
        spawn = Mock(return_value=child)
        monkeypatch.setattr(dashboard, "spawn_catchup", spawn)
        view = dashboard.PlanDashboard(notebook, tmp_path, lambda _: False)
        assert view.timer in window.tk.call("after", "info")
        window.update_idletasks()  # Run the real window-opening callback.
        spawn.assert_called_once_with(tmp_path, "--catch-up")
        assert "実行中" in view.recovery_label.get()
        assert view.accounts.get_children() == ("A", "B")
        assert "開始残高" in view.accounts.item("A", "values")
        assert "判定済・候補0" in view.accounts.item("B", "values")
        assert "最終判定 未判定" in view.detail.get()
        assert "日証金未着" in view.detail.get()
        assert view.recovery.item("daily", "values")[2] == "今回処理済み"
        view.start()
        view.start()
        assert spawn.call_count == 1
        child.poll.return_value = 2
        view.refresh()
        assert "途中で終了" in view.recovery_label.get()
        assert view.recovery.item("jsf", "values")[2] == "前回中断"
        (root / "summary.json").write_bytes(b"broken")
        view.refresh()
        assert not view.accounts.get_children() and "読込エラー" in view.checked.get()
    finally:
        window.destroy()


@pytest.mark.parametrize("state", ["completed", "external_worker", "unconfigured", "launch_error"])
def test_opening_rechecks_completed_work_and_respects_active_worker(tmp_path, monkeypatch, state):
    now = datetime(2026, 11, 19, 6, 30, tzinfo=dashboard.JST)
    monkeypatch.setattr(
        dashboard,
        "datetime",
        type("Clock", (datetime,), {"now": classmethod(lambda cls, tz: now)}),
    )
    if state != "unconfigured":
        save(tmp_path / "config/forward-plans.json", {"plans": []})
    save(
        tmp_path / "data/plan-forward/recovery/latest.json",
        dict(status="ok", target="2026-11-18", started_at=now.isoformat()),
    )
    spawn = Mock()
    spawn.return_value.poll.return_value = None
    if state == "launch_error":
        spawn.side_effect = OSError("unavailable Python")
    monkeypatch.setattr(dashboard, "spawn_catchup", spawn)
    window = create_window(tk)
    window.withdraw()
    try:
        notebook = ttk.Notebook(window)
        notebook.pack()
        view = dashboard.PlanDashboard(notebook, tmp_path, lambda _: state == "external_worker")
        window.update_idletasks()
        if state in {"external_worker", "unconfigured"}:
            spawn.assert_not_called()
        else:
            spawn.assert_called_once_with(tmp_path, "--catch-up")
        if state in {"external_worker", "completed"}:
            assert "実行中" in view.recovery_label.get()
        elif state == "launch_error":
            assert "起動できません" in view.recovery_label.get()
        # Refreshing the displayed state does not start a second worker.
        view.frame.after_cancel(view.timer)
        view.tick()
        assert spawn.call_count == (0 if state in {"external_worker", "unconfigured"} else 1)
        if state == "launch_error":
            spawn.side_effect = None
            view.start()
            assert spawn.call_count == 2
            assert "実行中" in view.recovery_label.get()
    finally:
        window.destroy()


def test_catchup_process_is_paper_cli_and_uses_project_env(tmp_path, monkeypatch):
    create = Mock()
    monkeypatch.setattr(dashboard.subprocess, "Popen", create)
    assert dashboard.spawn_catchup(tmp_path) == create.return_value
    args = create.call_args.args[0]
    assert args[0] == str(tmp_path / ".venv/Scripts/python.exe")
    assert args[-2:] == ["trading_system.plan_forward", "--catch-up"]
    assert create.call_args.kwargs["cwd"] == tmp_path


def test_preopen_gate_after_early_launch_resume_and_restart(tmp_path, monkeypatch):
    save(tmp_path / "config/forward-plans.json", {"plans": []})
    view = dashboard.PlanDashboard.__new__(dashboard.PlanDashboard)
    view.project, view.child, view.gate_day = tmp_path, None, None
    view.root = tmp_path / "data/plan-forward"
    view.lock_probe = lambda _: False
    view.plans = {}
    observed = {}
    running = False

    def refresh():
        view.last_values = ({}, None, observed, None, running or view.child is not None, "")

    view.refresh = refresh
    refresh()
    clock = Mock()
    monkeypatch.setattr(
        dashboard,
        "datetime",
        type("Clock", (datetime,), {"now": classmethod(lambda cls, tz: clock.now(tz))}),
    )
    spawn = Mock()
    spawn.return_value.poll.return_value = None
    monkeypatch.setattr(dashboard, "spawn_catchup", spawn)

    def tick(when, *, opening=False):
        clock.now.return_value = datetime.fromisoformat(when + "+09:00")
        view.check_morning(clock.now.return_value, opening=opening)

    tick("2026-11-19T07:30", opening=True)
    assert spawn.call_count == 1
    tick("2026-11-19T07:40")
    assert spawn.call_count == 1  # early worker has not finished
    view.child = None
    observed.update(status="ok", started_at="2026-11-19T07:30:01+09:00")
    refresh()
    tick("2026-11-19T07:42")
    tick("2026-11-19T07:55")
    assert spawn.call_count == 2
    observed["started_at"] = "2026-11-19T07:42:01+09:00"
    view.child, view.gate_day = None, None  # restart the GUI
    refresh()
    tick("2026-11-19T08:10", opening=True)
    assert spawn.call_count == 2
    tick("2026-11-20T07:40")
    assert spawn.call_count == 3
    view.child = None
    refresh()
    tick("2026-11-21T07:40")
    observed.update(target="2026-11-20", next_session="2026-11-24")
    tick("2026-11-23T07:40")
    assert spawn.call_count == 3


def test_retry_waits_five_minutes_runs_without_heartbeat_and_stops_at_open(tmp_path, monkeypatch):
    save(tmp_path / "config/forward-plans.json", {"plans": []})
    day = "2026-11-19"
    view = dashboard.PlanDashboard.__new__(dashboard.PlanDashboard)
    view.project = tmp_path
    view.gate_day = datetime.fromisoformat(day).date()
    view.schedule_label = Mock()
    view.plans = {"A": dict(start="2026-11-17", status="waiting_inputs", next_entry=day)}
    rec = dict(
        status="attention",
        target="2026-11-18",
        next_session=day,
        checked_at=day + "T07:42:00+09:00",
    )
    view.last_values = ({}, None, rec, None, False, "")
    view.last_attempt = datetime.fromisoformat(day + "T07:40:00+09:00")
    view.start = Mock()

    def tick(at):
        view.check_morning(datetime.fromisoformat(day + "T" + at + ":00+09:00"))

    tick("07:45")
    assert not view.start.called and "07:47" in view.schedule_label.set.call_args.args[0]
    tick("07:47")
    view.start.assert_called_once_with()
    view.start.reset_mock()
    view.plans["A"].update(status="waiting_close", decision_status="ready", picks=[])
    tick("07:55")
    view.plans["A"].update(status="stopped")
    tick("08:03")
    view.plans["A"].update(status="waiting_inputs")
    tick("09:00")
    assert not view.start.called
    assert dashboard.needs_preopen_retry(
        {
            "B": dict(
                start="2026-11-17",
                status="ready",
                last_decision="2026-11-17",
                next_entry="2026-11-18",
            )
        },
        rec,
        day,
    )


def test_evening_gate_and_external_completion_across_relaunch(tmp_path):
    save(tmp_path / "config/forward-plans.json", {"plans": []})
    view = dashboard.PlanDashboard.__new__(dashboard.PlanDashboard)
    view.project = tmp_path
    view.root = tmp_path / "data/plan-forward"
    view.evening_day = None
    view.last_values = ({}, None, {}, None, False, "")
    when = datetime.fromisoformat("2026-11-19T22:00:00+09:00")

    def spawn(action):
        assert action == "--evening-check"
        view.evening_day = when.date()

    view.start = Mock(side_effect=spawn)
    for hour, minute in [(18, 0), (20, 10), (21, 59)]:
        view.check_evening(when.replace(hour=hour, minute=minute))
    assert not view.start.called
    view.check_evening(when)
    view.check_evening(when.replace(minute=1))
    assert view.start.call_count == 1
    view.evening_day = None
    save(view.root / "recovery/evening/2026-11-19.json", {"status": "ok"})
    view.check_evening(when.replace(hour=23))
    assert view.start.call_count == 1


def test_grouped_recovery_displays_failing_source_date():
    assert (
        dashboard.step_result(
            dict(
                date="2026-11-18",
                reason="required_file_missing",
                dates={"2026-11-17": {"status": "error"}, "2026-11-18": {"status": "ready"}},
            )
        )
        == "2026-11-17　必要な保存ファイルが見つかりません"
    )


def test_scheduled_jobs_show_dated_evidence_and_missing_evening(tmp_path):
    target = "2026-11-18"
    now = datetime.fromisoformat("2026-11-19T07:40:00+09:00")
    rec = {"target": target, "next_session": "2026-11-19"}
    save(
        tmp_path / "data/jquants/daily-snapshots" / target / "last_collection.json",
        {"date": target, "state": "complete", "completed_at": "2026-11-18T09:05:00+00:00"},
    )
    text = dashboard.saved_evening_work(tmp_path, now, rec)
    assert "2026-11-18" in text and "日足 保存済み 18:05" in text and "引け処理 記録待ち" in text
    save(
        tmp_path / "data/plan-forward/recovery/evening/2026-11-18.json",
        {"status": "ok", "checked_at": "2026-11-18T11:15:00+00:00"},
    )
    assert "引け処理 完了 20:15" in dashboard.saved_evening_work(tmp_path, now, rec)


def test_morning_diagnosis_uses_dated_evidence_and_real_freeze_time(tmp_path):
    day = "2026-11-20"
    proof = dict(
        target=day,
        signal_date="2026-11-19",
        state="prior_inputs",
        label="朝に確定（前夜の入力あり）",
        started_at="2026-11-20T07:40:01+09:00",
        deadline="2026-11-20T09:00:00+09:00",
        decision_at="2026-11-20T07:48:13+09:00",
        plans={
            "A": dict(
                state="preexisting",
                label="前夜に確定済み",
                candidate_count=0,
                decision_at="2026-11-19T20:15:31+09:00",
            ),
            "B": dict(
                state="prior_inputs",
                label="朝に確定（前夜の入力あり）",
                source_note="信用残高は前夜取得",
                candidate_count=2,
                order_count=1,
                decision_at="2026-11-20T07:48:13+09:00",
            ),
        },
    )
    save(tmp_path / "morning-check/latest.json", proof)
    assert dashboard.morning_check(tmp_path, "2026-11-21")[0] == {}
    report, issue = dashboard.morning_check(tmp_path, day)
    title = dashboard.morning_evidence(report, day, issue)
    assert "朝に確定（前夜の入力あり）" in title and "07:48:13" in title and "09:00:00" in title
    assert "11/19 20:15:31" in dashboard.plan_evidence(report, "A")
    assert "候補 0銘柄" in dashboard.plan_evidence(report, "A")
    assert "予定 1件" in dashboard.plan_evidence(report, "B")
    changed = dict(proof, label="確認期限を過ぎました", state="missed")
    save(tmp_path / ("morning-check/" + day + ".json"), changed)
    assert dashboard.morning_check(tmp_path, day) == (changed, None)


def test_pending_at_open_becomes_unconfirmed_until_final_receipt_arrives():
    day = "2026-11-20"
    view = dashboard.PlanDashboard.__new__(dashboard.PlanDashboard)
    view.deadline_day = None
    view.last_values = ({}, None, {}, None, False)
    view.morning = dict(
        state="pending",
        label="入力待ち",
        target=day,
        plans={"A": dict(state="pending", label="入力待ち")},
    )
    view.start = Mock()
    before = datetime.fromisoformat(day + "T08:59:59+09:00")
    view.check_deadline(before)
    assert not view.start.called
    after = before.replace(hour=9, minute=0, second=0)
    view.check_deadline(after)
    view.check_deadline(after)
    assert view.start.call_count == 1
    assert "期限経過" in dashboard.morning_evidence(view.morning, day, None, True)
    assert "期限経過" in dashboard.plan_evidence(view.morning, "A", True)
    view.morning.update(state="met", label="朝の起動だけで確定")
    assert "期限経過" not in dashboard.morning_evidence(view.morning, day, None, True)


def test_holdings_tab_updates_prices_without_changing_account_snapshot(tmp_path, monkeypatch):
    window = create_window(tk)
    window.withdraw()
    try:
        notebook = ttk.Notebook(window)
        notebook.pack()
        view = dashboard.PlanDashboard(notebook, tmp_path, lambda _: False)
        data = dict(
            rows=[
                dict(
                    plan="A",
                    code="12340",
                    name="テスト銘柄",
                    shares="100",
                    entry_price="1000.125",
                    current_price="1020.5",
                    price_date="2026-11-20",
                    entry_date="2026-11-19",
                    note="",
                )
            ],
            status="A：保有1銘柄　　B：保有0銘柄",
            issue="",
        )
        reader = Mock(return_value=data)
        monkeypatch.setattr(view.holdings_reader, "read", reader)
        window.update_idletasks()
        assert [notebook.tab(tab, "text") for tab in notebook.tabs()] == [
            "A・Bの稼働状況",
            "保有銘柄",
        ]
        values = view.holdings.item(view.holdings.get_children()[0], "values")
        assert values == (
            "A",
            "1234",
            "テスト銘柄",
            "100",
            "1,000.125",
            "1,020.5",
            "2026-11-20",
            "2026-11-19",
            "",
        )
        assert "B：保有0銘柄" in view.holdings_status.get()
        reader.return_value = dict(data, rows=[dict(data["rows"][0], current_price=None)])
        view.refresh()
        assert view.holdings.item(view.holdings.get_children()[0], "values")[5] == "未確認"
        reader.return_value = dict(data, rows=[], status="A：保有0銘柄　　B：保有0銘柄")
        view.refresh()
        assert not view.holdings.get_children()
        assert "A：保有0銘柄" in view.holdings_status.get()
    finally:
        window.destroy()


def test_all_account_headings_show_selected_account_details_and_follow_updates(
    tmp_path, monkeypatch
):
    save(
        tmp_path / "data/plan-forward/summary.json",
        dict(
            plans={
                "A": dict(
                    name="基準③",
                    capital=3000000,
                    cash=2900000,
                    nav=3010100,
                    positions={"12340": {}},
                    last_mark="2026-11-19",
                    next_entry="2026-11-20",
                    status="waiting_close",
                    decision_status="ready",
                    last_decision="2026-11-19",
                    picks=[dict(code="12340", name="テスト銘柄")],
                    decision_recorded_at="2026-11-19T20:10:00+09:00",
                ),
                "B": dict(
                    name="A0-5",
                    capital=3000000,
                    cash=2999950,
                    nav=2999950,
                    positions=0,
                    last_mark="2026-11-20",
                    next_entry="2026-11-24",
                    status="waiting_inputs",
                    issue="入力待ち",
                ),
            }
        ),
    )
    window = create_window(tk)
    window.withdraw()
    try:
        notebook = ttk.Notebook(window)
        notebook.pack()
        view = dashboard.PlanDashboard(notebook, tmp_path, lambda _: False)
        holdings = dict(
            rows=[
                dict(
                    plan="A",
                    code="12340",
                    name="テスト銘柄",
                    shares="100",
                    entry_price="1000",
                    current_price="1020.5",
                    price_date="2026-11-20",
                    entry_date="2026-11-19",
                    note="",
                )
            ],
            status="A：保有1銘柄　　B：保有0銘柄",
            issue="",
        )
        reader = Mock(return_value=holdings)
        monkeypatch.setattr(view.holdings_reader, "read", reader)
        window.update_idletasks()
        expected = {
            "plan": "基準③",
            "state": "寄付候補確定",
            "nav": "純資産　3,010,100円",
            "net": "評価損益　10,100円",
            "hold": "テスト銘柄",
            "mark": "最終評価日　2026-11-19",
            "entry": "対象寄付日　2026-11-20",
        }

        def click(field):
            callback = view.accounts.heading(field, "command")
            assert callback
            window.tk.call(callback)
            assert view.detail_text.get("1.0", "end-1c") == view.detail.get()
            assert str(view.detail_text["state"]) == "disabled"

        for field, content in expected.items():
            click(field)
            assert content in view.detail.get()
            assert (
                view.detail_box.cget("text") == view.accounts.heading(field, "text") + " — Aプラン"
            )
        assert "保存済みの候補銘柄" in view.detail.get()

        # Selecting another account updates the same active field, without another heading click.
        view.accounts.selection_set("B")
        window.update()
        assert "対象寄付日　2026-11-24" in view.detail.get()
        assert view.detail_box.cget("text").endswith("Bプラン")
        click("net")
        assert "評価損益　-50円" in view.detail.get()
        click("hold")
        assert "現在の保有銘柄はありません。" in view.detail.get()
        assert "テスト銘柄" not in view.detail.get()

        view.accounts.selection_set("A")
        window.update()
        assert "1,020.5円" in view.detail.get()
        reader.return_value = dict(
            holdings, rows=[dict(holdings["rows"][0], current_price="1100.5")]
        )
        view.refresh()
        assert "1,100.5円" in view.detail.get()
        reader.return_value = dict(holdings, rows=[])
        view.refresh()
        assert "保有明細を確認できません。" in view.detail.get()
        assert "現在の保有銘柄はありません。" not in view.detail.get()
    finally:
        window.destroy()


def test_heading_clicked_before_accounts_arrive_keeps_its_selection(tmp_path):
    window = create_window(tk)
    window.withdraw()
    try:
        notebook = ttk.Notebook(window)
        notebook.pack()
        view = dashboard.PlanDashboard(notebook, tmp_path, lambda _: False)
        window.update_idletasks()
        window.tk.call(view.accounts.heading("net", "command"))
        assert "集計記録を待っています" in view.detail.get()
        save(
            tmp_path / "data/plan-forward/summary.json",
            dict(
                plans={"B": dict(name="B", status="waiting_inputs", capital=3000000, positions=0)}
            ),
        )
        view.refresh()
        assert view.detail_box.cget("text") == "評価損益 — Bプラン"
        assert "評価損益　—" in view.detail.get()
    finally:
        window.destroy()
