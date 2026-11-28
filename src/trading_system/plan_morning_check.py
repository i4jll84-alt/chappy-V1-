"""Measure morning readiness from each account's frozen decision and input receipts."""

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from trading_system.research import a0_5_book
from trading_system.storage.store import atomic_write, canonical

JST = ZoneInfo("Asia/Tokyo")
LABELS = {
    "guarded": "寄付前に確定・新規買い停止",
    "verified_cutoff": "寄付前に確定",
    "met": "朝の起動だけで寄付前に確定",
    "prior_inputs": "寄付前に確定・起動前の受領資料を使用",
    "evening_run": "寄付前に確定・前夜にも起動",
    "preexisting": "確定判定を使用",
    "pending": "寄付前の確定待ち",
    "missed": "寄付前に間に合わず",
    "unverified": "記録不足で確認できず",
    "outside": "対象外",
    "stopped": "停止指示あり",
}


def timestamp(value):
    if not value:
        return None
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("timestamp_without_offset")
    return stamp.astimezone(JST)


def read(path):
    return json.loads(path.read_bytes())


def apply_state(item, state, reason):
    item.update(state=state, label=LABELS[state], source_note=reason)
    return item


def official(project, key, signal, target, spec):
    if key in ("A", "A9", "A10", "C") or spec.get("adapter") == "plan_b_v21":
        path = project / "data/plan-forward/accounts" / key / "sessions" / target / "decision.json"
        return read(path) if path.is_file() else None
    capital = spec["capital"]
    account = project / "data/a0-5-paper/accounts" / str(capital)
    db = account / "ledger.sqlite"
    if not db.is_file():
        raise ValueError("native_ledger_missing")
    anchor = read(project / "data/plan-forward/accounts/B/baseline.json")
    if (
        anchor["start"] != spec["start"]
        or anchor["initial_capital"] != capital
        or anchor["native_revision_sha256"]
        != a0_5_book.revision_digest(db, anchor["native_revision"])
    ):
        raise ValueError("native_baseline_mismatch")
    return a0_5_book.load(account, capital)["plans"].get(signal)


def market_receipts(project, signal, refs):
    root = project / "data/jquants"
    base = (root / "daily-snapshots" / signal).resolve()
    manifest_ref = next(r for r in refs if r.get("date") == signal and "manifest" in r)
    manifest = (root / manifest_ref["manifest"]).resolve()
    data = manifest.read_bytes() if manifest.is_relative_to(base) else b""
    if hashlib.sha256(data).hexdigest() != manifest_ref["sha256"]:
        raise ValueError("daily_manifest_revision_missing")
    pages = json.loads(data)["pages"]
    if not pages or any(not p.get("observed_at") or not p.get("available_at") for p in pages):
        raise ValueError("price_observation_missing")
    receipt = read(base / "last_collection.json")
    if receipt["date"] != signal or Path(receipt["daily_manifest"]).resolve() != manifest:
        raise ValueError("daily_receipt_revised")
    job = receipt["jobs"]["master"]["job_id"]
    expected = {r["page"]: r["sha256"] for r in refs if r.get("master_job") == job}
    if not expected:
        raise ValueError("master_receipt_missing")
    catalog = root / "reference/master/catalog.duckdb"
    with duckdb.connect(str(catalog), read_only=True) as db:
        rows = db.execute(
            "SELECT raw_path,raw_sha256,observed_at FROM pages WHERE job_id=?", [job]
        ).fetchall()
    if {p: h for p, h, _ in rows} != expected or any(t is None for _, _, t in rows):
        raise ValueError("master_observation_mismatch")
    return [*pages, *[dict(observed_at=t.isoformat()) for _, _, t in rows]]


def source_timing(project, key, doc, signal, start, cutoff):
    refs = doc.get("price_and_master_sources" if key == "A" else "inputs", [])
    daily = [
        r
        for r in refs
        if r.get("date") == signal and ("manifest" in r if key == "A" else "daily_receipt" in r)
    ]
    required = [r.get("completed_at") for r in daily]
    if not required:
        return "unverified", "対象日の価格受領時刻が未確認"
    aux = []
    if key == "A":
        aux = doc.get("jsf_sources", [])
        if not any(r.get("date", signal) < signal and r.get("available_at") for r in aux):
            return "unverified", "日証金の比較に使った前日資料の受領時刻が未確認"
        jsf_today = [r.get("available_at") for r in aux if r.get("date") == signal]
        if not jsf_today:
            return "unverified", "対象日の日証金の受領時刻が未確認"
        required.extend(jsf_today)
    if key == "B":
        daily_refs = [
            r
            for r in refs
            if r.get("date") == signal and r.get("dataset") in ("financials", "topix")
        ]
        if {r["dataset"] for r in daily_refs} != {"financials", "topix"}:
            return "unverified", "対象日の財務・TOPIXの受領記録が未確認"
        required.extend(r.get("observed_at") for r in daily_refs)
    if key == "B" and (doc.get("candidates") or doc.get("holding_sectors")):
        earnings = doc.get("earnings_input") or {}
        if any("手動" in str(r.get("earnings_source", "")) for r in doc.get("candidates", [])):
            return "unverified", "手動確認した決算予定の受領時刻が未確認"
        if not earnings.get("available_at"):
            return "unverified", "決算予定の受領時刻が未確認"
        aux = [earnings]

    if any(not ts for ts in required):
        return "unverified", "必須資料の受領時刻が未確認"
    stamps = [timestamp(ts) for ts in required]
    evening = datetime.combine(date.fromisoformat(signal), time(15, 30), JST)
    evidence = [*refs, *aux, *doc.get("calendar_sources", [])]
    seen = [
        timestamp(r[k])
        for r in evidence
        for k in ("available_at", "completed_at", "observed_at")
        if r.get(k)
    ]
    if any(ts > cutoff for ts in stamps + seen):
        return "unverified", "入力受領時刻が判定締切より後"
    if min(stamps) < start or any(evening <= ts < start for ts in seen):
        return "prior_inputs", "前営業日の引け以降、朝の起動前に受領した資料あり"
    raw = market_receipts(project, signal, refs)
    raw_stamps = [timestamp(r[k]) for r in raw for k in ("observed_at", "available_at") if r.get(k)]
    if max(raw_stamps) > cutoff:
        return "unverified", "価格・銘柄情報の受領時刻が判定締切より後"
    if min(raw_stamps) < start:
        return "prior_inputs", "朝の起動前に受領済みの対象日価格・銘柄情報を使用"
    return "met", "対象日の日次入力を朝の起動後に受領し、確定に使用"


def assess_plan(project, key, spec, signal, target, start, deadline, checked):
    item = dict(decision_at=None, seconds_to_open=None, candidate_count=None, order_count=None)
    if not spec or spec["stage"] != "active" or signal < spec["start"]:
        return apply_state(item, "outside", "比較対象の開始前または非稼働")
    try:
        doc = official(project, key, signal, target, spec)
        family = "A" if key in ("A9", "A10") or spec.get("adapter") == "plan_b_v21" else key
        if doc is None:
            return apply_state(
                item, "pending" if checked < deadline else "missed", "対象日の確定記録なし"
            )
        if (key in ("A9", "A10") or spec.get("adapter") == "plan_b_v21") and doc.get(
            "strategy"
        ) != spec["version"]:
            return apply_state(item, "unverified", "口座と保存判定のルール版が一致しない")
        pead = key == "C" and spec.get("adapter") == "plan_c_pead_forward"
        if pead and (
            doc.get("version") != spec["version"] or doc.get("strategy") != "GRID_DIP_H60_ATR40"
        ):
            return apply_state(item, "unverified", "Cの確定記録と現在のPEADルール版が一致しない")
        item["decision_sha256"] = hashlib.sha256(canonical(doc).encode()).hexdigest()
        ds, dt = (
            (doc.get("signal_date"), doc.get("target"))
            if family in ("A", "C")
            else (doc.get("date"), doc.get("order_date"))
        )
        state = doc.get("status" if family == "A" else "state")
        if ds != signal or (dt != target and not (dt is None and state == "blocked")):
            return apply_state(item, "unverified", "選定日・対象寄付日が一致しない")
        if state in ("disabled", "stopped"):
            return apply_state(item, "stopped", "判定時の停止を記録")
        if state == "missed_preopen":
            return apply_state(item, "missed", "寄付前判定の欠測を記録")
        if state not in ("ready", "blocked", "insufficient_data") and not (
            pead and state == "exits_only"
        ):
            return apply_state(item, "unverified", "確定状態が未確認")
        if state == "blocked":
            return apply_state(
                item, "pending" if checked < deadline else "missed", "必要な入力が未確定"
            )

        picks = doc["picks" if pead else {"A": "picks", "B": "candidates", "C": "ranked"}[family]]
        item["candidate_count"] = len(doc.get("ranked", picks) if pead else picks)
        if key == "B":
            item["order_count"] = len(picks) if family == "A" else len(doc["orders"])
        elif key == "C":
            item["order_count"] = len(doc["plan"]["buys"])
        timed = timestamp(doc.get("recorded_at"))
        cutoff = timestamp(doc.get("cutoff"))
        if not timed:
            if (
                key == "B"
                and cutoff
                and cutoff.date().isoformat() == signal
                and not doc.get("decision_origin")
            ):
                return apply_state(
                    item, "preexisting", "前営業日判定の保存版。確定完了時刻は旧形式で未収録"
                )
            return apply_state(item, "unverified", "確定完了時刻が未確認")
        if timed > checked or (cutoff and cutoff > timed):
            return apply_state(item, "unverified", "確定時刻の整合性を確認できない")
        item.update(
            decision_at=timed.isoformat(),
            seconds_to_open=math.floor((deadline - timed).total_seconds()),
        )
        if timed >= deadline:
            return apply_state(item, "missed", "確定完了が09:00以降")
        if key == "C" and state == "insufficient_data":
            return apply_state(item, "guarded", "資料不足による判定不可。既存建玉の出口は継続")
        if pead and not doc["allow_entries"]:
            if item["order_count"]:
                return apply_state(item, "unverified", "新規停止の判定に買付予定が含まれています")
            return apply_state(item, "guarded", "対象寄付の新規買付はPEADの条件により停止")
        if timed < start:
            return apply_state(item, "preexisting", "朝の初回起動より前に確定済み")
        if cutoff is None:
            return apply_state(item, "unverified", "入力締切を確認できない")
        if doc.get("decision_origin") == "evening_inputs":
            return apply_state(item, "prior_inputs", "前営業日までにそろった入力で朝に確定")
        if key == "C":
            # The C receipts cover a longer historical window than the A/B morning audit.
            return apply_state(item, "verified_cutoff", "保存された締切時点の資料から寄付前に確定")
        return apply_state(item, *source_timing(project, family, doc, signal, start, cutoff))
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        IndexError,
        AttributeError,
        StopIteration,
        sqlite3.Error,
        duckdb.Error,
    ):
        return apply_state(item, "unverified", "確定記録・入力の検証に失敗")


def record_startup(project, report, *, now=None):
    """Called under the dispatch lock after forward processing and before optional supplements."""
    project = Path(project)
    checked = (now or datetime.now(JST)).astimezone(JST)
    recovery = report.get("recovery", {})
    target, signal = recovery.get("next_session"), recovery.get("target")
    start = timestamp(recovery.get("started_at"))
    day = checked.date().isoformat()
    folder = project / "data/plan-forward/morning-check"
    path = folder / (day + ".json")
    previous = read(path) if path.is_file() else {}
    base = dict(
        target=target,
        signal_date=signal,
        checked_at=checked.isoformat(),
        started_at=None,
        deadline=None,
        decision_at=None,
        seconds_to_open=None,
        plans={},
    )
    if not start or start.date() != checked.date() or target != day or start.time() >= time(15, 30):
        return previous or apply_state(base, "outside", "対象の朝起動時間帯外")
    if start > checked:
        return previous or apply_state(base, "unverified", "起動時刻が観測時刻より後")
    if not signal or not date.fromisoformat(signal) < checked.date():
        return previous or apply_state(base, "unverified", "対象となる前営業日を確認できない")
    if previous.get("signal_date") == signal and previous.get("target") == target:
        first = timestamp(previous["started_at"])
        start = min(start, first)
    deadline = datetime.combine(checked.date(), time(9), JST)
    specs = {p["id"]: p for p in read(project / "config/forward-plans.json")["plans"]}
    rows = {
        k: assess_plan(project, k, specs.get(k), signal, target, start, deadline, checked)
        for k in ("A", "B")
    }
    for key in ("C", "A9", "A10"):
        spec = specs.get(key)
        if spec and spec["stage"] == "active" and signal >= spec["start"]:
            rows[key] = assess_plan(project, key, spec, signal, target, start, deadline, checked)
    b_account = project / "data/a0-5-paper/accounts" / str(specs.get("B", {}).get("capital", 0))
    switches = {
        "C": (project / "data/plan-forward/accounts/C/STOP").exists()
        or (project / "data/plan-forward/STOP").exists()
        or (
            project / "data/plan-forward/accounts/C/sessions" / target / "preopen_stop.json"
        ).exists(),
        "B": (
            (project / "data/a0-5-paper/STOP").exists()
            or (project / "data/a0-5-paper/stops" / (target + ".json")).exists()
            or (b_account / "stops" / (target + ".json")).exists()
            or not (project / "data/a0-5-paper/enabled").is_file()
        ),
    }
    tracked = ["A", "A9", "A10"]
    if specs.get("B", {}).get("adapter") == "plan_b_v21":
        tracked.append("B")
    for key in tracked:
        account = project / "data/plan-forward/accounts" / key
        switches[key] = (account / "STOP").exists() or (
            account / "sessions" / target / "preopen_stop.json"
        ).exists()
    for key, item in rows.items():
        if item["state"] != "outside" and (
            switches[key] or (key != "C" and (project / "data/swing-forward/STOP").exists())
        ):
            item["readiness_state"] = item["state"]
            apply_state(item, "stopped", item["label"] + "。ペーパー停止・無効化の記録あり")
    states = {r["state"] for r in rows.values()}
    grade = next(
        s
        for s in (
            "stopped",
            "outside",
            "missed",
            "unverified",
            "pending",
            "guarded",
            "verified_cutoff",
            "preexisting",
            "prior_inputs",
            "met",
        )
        if s in states
    )
    if (
        previous.get("state") in ("met", "prior_inputs", "preexisting", "evening_run")
        and previous.get("plans", {}).keys() == rows.keys()
        and grade in ("pending", "unverified", "missed")
    ):
        return previous
    base.update(started_at=start.isoformat(), deadline=deadline.isoformat(), plans=rows)
    if all(r["decision_at"] for r in rows.values()):
        final = max(timestamp(r["decision_at"]) for r in rows.values())
        base.update(
            decision_at=final.isoformat(),
            seconds_to_open=math.floor((deadline - final).total_seconds()),
        )
    apply_state(base, grade, "／".join(f"{k} {r['label']}" for k, r in rows.items()))
    if (
        grade == "met"
        and (project / "data/plan-forward/recovery/evening" / (signal + ".json")).is_file()
    ):
        apply_state(
            base, "evening_run", "朝の開始後に資料を受領し確定。前営業日にも夜間処理の起動記録あり"
        )
    atomic_write(path, canonical(base).encode(), sharing_retries=3)
    latest = folder / "latest.json"
    if not latest.exists() or read(latest)["target"] <= target:
        atomic_write(latest, canonical(base).encode(), sharing_retries=3)
    return base


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--date", type=date.fromisoformat)
    opts = parser.parse_args(argv)
    filename = (opts.date.isoformat() if opts.date else "latest") + ".json"
    path = opts.project / "data/plan-forward/morning-check" / filename
    result = read(path) if path.is_file() else apply_state({}, "unverified", "朝起動の実測記録なし")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
