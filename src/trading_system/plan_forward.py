"""Run the selected prospective paper strategies and publish their comparable account marks."""

import argparse
import importlib
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from trading_system.brokers.locking import single_writer
from trading_system.plan_retirement import (
    retire_fixed_comparisons,
    retire_for_v21,
    retire_previous_c,
)
from trading_system.storage.store import atomic_write, canonical

JST = ZoneInfo("Asia/Tokyo")
RESEARCH_PACKAGE = "trading_system.research."
STATUS_LABELS = {
    "waiting_signal": "引けの選定待ち",
    "waiting_calendar": "取引カレンダー待ち",
    "waiting_inputs": "株価・日証金データ待ち",
    "pead_inputs": "PEADの価格・決算開示待ち",
    "exits_only": "売却条件を確定・新規買付なし",
    "waiting_close_inputs": "評価用の日足待ち",
    "waiting_close": "翌取引日の評価待ち",
    "awaiting_native_decision": "A0-5の日次判定待ち",
    "missing_native_decision": "A0-5の判定欠測",
    "disabled": "口座が無効",
    "missed_preopen": "寄付前判定が欠測",
    "stopped_preopen": "寄付前停止・新規買付なし",
    "waiting_for_first_close": "初回の引け待ち",
    "waiting_for_close": "引け待ち",
    "waiting_for_signal": "選定待ち",
    "waiting_for_inputs": "データ待ち",
    "waiting_for_open": "寄付待ち",
    "pending": "待機中",
    "ready": "判定確定",
    "settled": "精算済み",
    "stopped": "停止",
    "blocked": "要確認",
    "needs_review": "要確認",
    "error": "更新エラー",
}


def read(path):
    return json.loads(path.read_bytes())


def save(path, value):
    atomic_write(path, canonical(value).encode(), sharing_retries=3)


def catalog(project):
    plans = read(project / "config/forward-plans.json")["plans"]
    seen = set()
    for p in plans:
        key = p["id"]
        if key in seen or not re.fullmatch("[A-Z][A-Z0-9_-]{0,31}", key):
            raise ValueError(f"invalid or duplicate plan id: {key}")
        seen.add(key)
        if not p["name"] or p["stage"] not in ("research", "active", "retired"):
            raise ValueError(f"invalid stage or name: {key}")
        if p["stage"] == "active":
            if (
                type(p["capital"]) is not int
                or p["capital"] <= 0
                or not p["version"]
                or not re.fullmatch("[a-z][a-z0-9_]*", p["adapter"])
                or date.fromisoformat(p["start"]).isoformat() != p["start"]
            ):
                raise ValueError(f"invalid account specification: {key}")
    return plans


def account_spec(plan):
    return {key: plan[key] for key in ("id", "adapter", "version", "start", "capital")}


def marked_plans(report):
    for key, plan in report["plans"].items():
        positions = plan.get("positions")
        if isinstance(positions, (list, dict)):
            positions = len(positions)
        nav = plan.get("nav")
        net = None if nav is None else Decimal(str(nav)) - Decimal(plan["capital"])
        yield key, plan, positions, net


def display_status(plan):
    state = plan.get("status") or "未確認"
    decision = plan.get("decision_status") or plan.get("decision_state")
    if state == "waiting_close" and decision in ("missed_preopen", "stopped", "stopped_preopen"):
        return decision
    if plan.get("adapter") == "plan_c500" and state == "waiting_close" and decision == "exits_only":
        return "exits_only"
    if plan.get("adapter") == "plan_c_pead_forward":
        if state == "waiting_inputs":
            return "pead_inputs"
        if state in ("waiting_for_open", "waiting_close") and decision == "exits_only":
            return "exits_only"
    return state


def brief_status(report):
    """Project the saved status only. Keep missing, null and false observations distinct."""
    fields = (
        "version",
        "status",
        "processing_status",
        "last_mark",
        "holdings_date",
        "last_decision",
        "next_signal",
        "next_entry",
        "decision_status",
        "decision_state",
        "decision_cutoff",
        "decision_recorded_at",
        "decision_origin",
        "entry_decision_pending",
        "allow_entries",
        "coverage_ok",
        "coverage_counts",
        "valuation_complete",
        "issue",
        "reason",
        "no_buy_reason",
        "regime_reason",
        "target_budget",
        "candidate_count",
        "order_count",
        "morning_recovery",
        "review_status",
        "review_count",
        "last_review",
    )
    plans = {}
    for key, row in report["plans"].items():
        brief = {field: row[field] for field in fields if field in row}
        review = row.get("last_review")
        if isinstance(review, dict) and isinstance(review.get("issues"), list):
            brief["last_review"] = {
                "date": review.get("date"),
                "issue_count": len(review["issues"]),
            }
        if "entitlement_review" in row:
            issues = row["entitlement_review"]
            brief["entitlement_review_count"] = len(issues) if isinstance(issues, list) else None
        plans[key] = brief
    return {**report, "plans": plans}


def publish_page(root, report):
    def yen(v):
        return "—" if v is None else f"{Decimal(str(v)):,.0f}円"

    def cell(v):
        return f"<td>{escape(str(v)) if v is not None else '—'}</td>"

    rows = []
    for key, plan, positions, net in marked_plans(report):
        status = display_status(plan)
        fields = (
            f"{key}　{plan['name']}",
            plan["start"],
            yen(plan["capital"]),
            yen(plan.get("cash")),
            positions,
            yen(plan.get("nav")),
            yen(net),
            plan.get("last_mark"),
            plan.get("next_entry"),
            STATUS_LABELS.get(status, status),
        )
        rows.append("<tr>" + "".join(cell(v) for v in fields) + "</tr>")
    headings = (
        "プラン",
        "開始日",
        "元手",
        "現金",
        "保有数",
        "純資産",
        "評価損益",
        "最終評価日",
        "次の対象寄付",
        "状態",
    )
    header = "".join(f"<th>{h}</th>" for h in headings)
    html = f"""<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>フォワードテスト</title>
<style>body{{font:16px system-ui,sans-serif;color:#182b39;margin:3%}}
table{{border-collapse:collapse}}
td,th{{padding:.7em;border-bottom:1px solid #ccd7dd;text-align:right}}
td:first-child,th:first-child{{text-align:left}}th{{background:#eff4f6}}div{{overflow-x:auto}}</style>
<h1>フォワードテスト</h1><p>ペーパー口座 ｜ 更新 {escape(report["checked_at"])}</p>
<div><table><thead><tr>{header}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>
<p>各プランの現金・建玉は独立。純資産と評価損益は各口座の最終評価時点の値です。</p></html>
"""
    atomic_write(root / "summary.html", html.encode(), sharing_retries=3)


def run_due(project, *, now=None, recovery_signal=None):
    project = Path(project).resolve()
    realtime = now is None
    now = now or datetime.now(JST)
    started = monotonic()
    if now.utcoffset() is None:
        raise ValueError("timezone required")
    now = now.astimezone(JST)

    def current_time():
        if realtime:
            return datetime.now(JST)
        return now + timedelta(seconds=monotonic() - started)

    root = project / "data/plan-forward"
    try:
        with single_writer(root):
            plans = catalog(project)
            report = {"status": "ok", "checked_at": now.isoformat(), "plans": {}}
            retire_fixed_comparisons(root, plans, current_time())
            retire_for_v21(project, root, plans, current_time())
            for plan in plans:
                if plan["stage"] != "active":
                    continue
                key = plan["id"]
                spec = account_spec(plan)
                book = root / "accounts" / key
                row = {"name": plan["name"], **spec}
                try:
                    adapter = importlib.import_module(RESEARCH_PACKAGE + plan["adapter"])
                    retire_previous_c(root, spec, current_time())
                    enrollment = book / "enrollment.json"
                    enrolled = enrollment.exists()
                    if enrolled:
                        if read(enrollment)["spec"] != spec:
                            raise ValueError("account_terms_changed")
                    options_for = getattr(adapter, "account_options", None)
                    options = options_for(plan) if options_for is not None else {}
                    if not enrolled:
                        save(enrollment, {"spec": spec, "registered_at": now.isoformat()})
                    recover_plan = getattr(adapter, "recover_morning", None)
                    current = current_time()
                    if (
                        recover_plan is not None
                        and recovery_signal
                        and plan["start"] <= recovery_signal < current.date().isoformat()
                    ):
                        try:
                            row["morning_recovery"] = recover_plan(
                                project,
                                book,
                                signal_date=recovery_signal,
                                start=plan["start"],
                                capital=plan["capital"],
                                now=current,
                                **options,
                            )
                        except Exception as exc:
                            row["morning_recovery"] = {
                                "state": "error",
                                "error_type": type(exc).__name__,
                            }
                    snapshot = adapter.advance(
                        project,
                        book,
                        start=plan["start"],
                        capital=plan["capital"],
                        now=current_time(),
                        **options,
                    )
                    if (
                        not snapshot.get("status")
                        or Decimal(str(snapshot["initial_capital"])) != plan["capital"]
                    ):
                        raise ValueError("invalid_account_summary")
                    row.update(snapshot)
                    row.update(spec)
                    canonical(row)
                except Exception as exc:
                    reason = (
                        str(exc)
                        if str(exc)
                        in (
                            "account_terms_changed",
                            "invalid_account_summary",
                            "plan_a_version_mismatch",
                        )
                        else type(exc).__name__
                    )
                    row = {"name": plan["name"], **spec, "status": "error", "issue": reason}
                if (
                    row["status"] == "error"
                    or row.get("morning_recovery", {}).get("state") == "error"
                ):
                    report["status"] = "error"
                elif (
                    row["status"]
                    in (
                        "blocked",
                        "needs_review",
                        "stopped",
                        "disabled",
                        "missing_native_decision",
                        "missed_preopen",
                    )
                    and report["status"] == "ok"
                ):
                    report["status"] = "attention"
                report["plans"][key] = row
            save(root / "daily" / (now.date().isoformat() + ".json"), report)
            save(root / "summary.json", report)
            publish_page(root, report)
            return report
    except ValueError as exc:
        if str(exc) == "broker_state_already_open":
            return {"status": "busy", "plans": {}}
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    for action in ("due", "status", "list", "catch-up", "evening-check", "scheduled-close"):
        actions.add_argument("--" + action, action="store_true")
    actions.add_argument("--status-brief", action="store_true", help="read a compact saved status")
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[2])
    opts = parser.parse_args(argv)
    if opts.scheduled_close:
        from trading_system.plan_schedule import run_scheduled_close

        result = run_scheduled_close(opts.project)
    elif opts.evening_check:
        from trading_system.plan_evening import run_evening

        result = run_evening(opts.project)
    elif opts.catch_up:
        from trading_system.plan_startup import run_startup

        result = run_startup(opts.project)
    elif opts.due:
        result = run_due(opts.project)
    elif opts.list:
        result = {"plans": catalog(opts.project)}
    else:
        path = opts.project / "data/plan-forward/summary.json"
        result = read(path) if path.exists() else {"status": "not_started", "plans": {}}
    if opts.status_brief:
        print(json.dumps(brief_status(result), ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("status") in ("error", "attention") else 0


if __name__ == "__main__":
    raise SystemExit(main())
