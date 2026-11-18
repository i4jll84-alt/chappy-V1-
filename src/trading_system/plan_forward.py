"""Run the selected prospective paper strategies and publish their comparable account marks."""

import argparse
import importlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_system.brokers.locking import single_writer
from trading_system.storage.store import atomic_write, canonical

JST = ZoneInfo("Asia/Tokyo")
RESEARCH_PACKAGE = "trading_system.research."
STATUS_LABELS = {
    "waiting_signal": "引けの選定待ち",
    "waiting_calendar": "取引カレンダー待ち",
    "waiting_inputs": "株価・日証金データ待ち",
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
        if not p["name"] or p["stage"] not in ("research", "active"):
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


def publish_page(root, report):
    def yen(v):
        return "—" if v is None else f"{Decimal(str(v)):,.0f}円"

    def cell(v):
        return f"<td>{escape(str(v)) if v is not None else '—'}</td>"

    rows = []
    for key, plan, positions, net in marked_plans(report):
        status = plan["status"]
        if status == "waiting_close" and plan.get("decision_status") in (
            "stopped",
            "missed_preopen",
            "stopped_preopen",
        ):
            status = plan["decision_status"]
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


def run_due(project, *, now=None):
    project = Path(project).resolve()
    realtime = now is None
    now = now or datetime.now(JST)
    if now.utcoffset() is None:
        raise ValueError("timezone required")
    now = now.astimezone(JST)
    root = project / "data/plan-forward"
    plans = catalog(project)
    try:
        with single_writer(root):
            report = {"status": "ok", "checked_at": now.isoformat(), "plans": {}}
            for plan in plans:
                if plan["stage"] == "research":
                    continue
                key = plan["id"]
                spec = account_spec(plan)
                book = root / "accounts" / key
                row = {"name": plan["name"], **spec}
                try:
                    enrollment = book / "enrollment.json"
                    if enrollment.exists():
                        if read(enrollment)["spec"] != spec:
                            raise ValueError("account_terms_changed")
                    else:
                        save(enrollment, {"spec": spec, "registered_at": now.isoformat()})
                    adapter = importlib.import_module(RESEARCH_PACKAGE + plan["adapter"])
                    snapshot = adapter.advance(
                        project,
                        book,
                        start=plan["start"],
                        capital=plan["capital"],
                        now=datetime.now(JST) if realtime else now,
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
                        if str(exc) in ("account_terms_changed", "invalid_account_summary")
                        else type(exc).__name__
                    )
                    row = {"name": plan["name"], **spec, "status": "error", "issue": reason}
                if row["status"] == "error":
                    report["status"] = "error"
                elif (
                    row["status"]
                    in ("blocked", "needs_review", "stopped", "disabled", "missing_native_decision")
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
    for action in ("due", "status", "list"):
        actions.add_argument("--" + action, action="store_true")
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[2])
    opts = parser.parse_args(argv)
    if opts.due:
        result = run_due(opts.project)
    elif opts.list:
        result = {"plans": catalog(opts.project)}
    else:
        path = opts.project / "data/plan-forward/summary.json"
        result = read(path) if path.exists() else {"status": "not_started", "plans": {}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("status") in ("error", "attention") else 0


if __name__ == "__main__":
    raise SystemExit(main())
