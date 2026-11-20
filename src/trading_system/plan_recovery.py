"""Recover missing after-close paper inputs at their actual observation times."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic, sleep

from trading_system.brokers.locking import single_writer
from trading_system.plan_schedule import EVENING_CHECK
from trading_system.research import a0_5, a0_5_inputs, swing_credit, swing_daily
from trading_system.research import swing_earnings as earnings
from trading_system.research import swing_short_sources as shorts
from trading_system.research.daytrade_universe import JST, calendar_days, dated_inputs
from trading_system.research.daytrade_universe import prepare as prepare_panel
from trading_system.settings import Settings
from trading_system.storage.store import atomic_write, canonical

GOOD = {"ready", "recovered", "empty", "skipped", "missed"}


def save(path, record):
    atomic_write(path, canonical(record).encode(), sharing_retries=3)


def read(path):
    return json.loads(path.read_bytes())


def clock(now):
    if now is None:
        return lambda: datetime.now(JST)
    if now.utcoffset() is None:
        raise ValueError("timezone_required")
    before = monotonic()
    return lambda: now.astimezone(JST) + timedelta(seconds=monotonic() - before)


def at(day, when):
    return datetime.combine(date.fromisoformat(day), when, JST)


def evening_threshold(day, now):
    due = at(day, EVENING_CHECK)
    return due if now >= due else None


def result(status, **fields):
    return dict(status=status, **fields)


def explain(exc):
    # Keep provider bodies, command lines and credentials out of UI and process output.
    if isinstance(exc, FileNotFoundError):
        return "required_file_missing"
    if isinstance(exc, ValueError):
        reason = str(exc)
        if reason.isascii() and reason.replace("_", "").isalnum() and len(reason) < 70:
            return reason
    return type(exc).__name__


def grouped(days, action):
    statuses = {}
    for day in sorted(set(days)):
        try:
            statuses[day] = action(day)
        except Exception as exc:
            statuses[day] = result("error", reason=explain(exc))
    failed = next((r for r in statuses.values() if r["status"] not in GOOD), None)
    return dict(
        failed
        or result(
            "recovered" if any(r["status"] == "recovered" for r in statuses.values()) else "ready"
        ),
        dates=statuses,
    )


def coverage(project, now, root):
    jq = project / "data/jquants"
    days, _ = calendar_days(jq, now.date(), now)
    latest = now.date() if now.time() >= EVENING_CHECK else now.date() - timedelta(days=1)
    while days[latest.isoformat()] not in ("1", "2"):
        latest -= timedelta(days=1)
    sessions = sorted(d for d, f in days.items() if f in ("1", "2"))
    target = latest.isoformat()
    next_day = sessions[sessions.index(target) + 1]
    plans = read(project / "config/forward-plans.json")["plans"]
    start = min((p["start"] for p in plans if p["stage"] == "active"), default=target)
    last = root / "progress.json"
    first = min(target, start)
    archived = read(jq / "history/quality-20261031.json")["end"]
    database = jq / "history/history.sqlite"
    with ro(database) as db:
        last_filing = db.execute(
            "SELECT max(max_date) FROM bulk_files WHERE endpoint=? AND state='complete'",
            (a0_5_inputs.SCHEMA["financial"][0],),
        ).fetchone()[0]
    if not last_filing:
        raise ValueError("financial_archive_coverage_unknown")
    if last.is_file():
        first = max(first, min(target, read(last)["through"]))
    else:
        # B checks every post-bulk financial reporting day, including days older than its
        # 29-session price window. Verify the whole bridge once, then advance a contiguous cursor.
        gap_start = date.fromisoformat(min(archived, last_filing)) + timedelta(days=1)
        first = min(
            first,
            max(gap_start, latest - timedelta(days=a0_5_inputs.FINANCIAL_HISTORY_DAYS)).isoformat(),
        )
    begin = date.fromisoformat(first)
    # calendar_days spans 30 previous and 20 future days. Include a warm-up for new/late ledgers.
    cursor = begin - timedelta(days=52)
    while cursor < latest:
        more, _ = calendar_days(jq, cursor, now)
        days.update(more)
        cursor += timedelta(days=35)
    begin_calendar = begin - timedelta(days=49)
    for n in range((latest - begin_calendar).days + 1):
        if (begin_calendar + timedelta(days=n)).isoformat() not in days:
            raise ValueError("incomplete_recovery_calendar")
    sessions = sorted(d for d, f in days.items() if f in ("1", "2") and d <= target)
    missing_span = [d for d in sessions if first <= d]
    if not missing_span:
        raise ValueError("missing_recovery_session")
    # For the first signal from a restored window, prices before the bulk archive end
    # may come from the original archive. Newer sessions require dated snapshots.
    begin_idx = sessions.index(missing_span[0])
    warm = sessions[max(0, begin_idx - 28) : begin_idx]
    warm = [
        d
        for d in warm
        if d > archived
        and not daily_receipt_complete(jq / "daily-snapshots" / d / "last_collection.json", d)
    ]
    previous = sessions[begin_idx - 1] if begin_idx else missing_span[0]
    return (
        target,
        next_day,
        sorted(set(warm + missing_span)),
        sorted(set([previous, *missing_span])),
    )


def daily_receipt_complete(path, day):
    if not path.is_file():
        return False
    receipt = read(path)
    if receipt.get("date") != day:
        raise ValueError("wrong_daily_receipt_date")
    return bool(
        receipt.get("daily_manifest")
        and receipt.get("completed_at")
        and receipt.get("state") == "complete"
        and all(
            receipt.get("jobs", {}).get(k, {}).get("state") in allowed
            for k, allowed in {
                "calendar": {"complete"},
                "master": {"complete"},
                "topix": {"complete"},
                "financials": {"empty", "complete"},
            }.items()
        )
    )


def daily(project, day, now):
    jq = project / "data/jquants"
    path = jq / "daily-snapshots" / day / "last_collection.json"
    missing = not daily_receipt_complete(path, day)
    if missing:
        settings = Settings.load(project / "config/stage1.toml")
        if (project / settings.data_root).resolve() != jq.resolve():
            raise ValueError("project_data_root_mismatch")
        settings = settings.model_copy(update={"data_root": jq})
        swing_daily.collect(date.fromisoformat(day), settings, run_paper=False)
    receipt = read(path)
    if not daily_receipt_complete(path, day):
        raise ValueError("incomplete_daily_jobs")
    dated_inputs(jq, [day], now())
    for dataset, column in (("financials", "DiscDate"), ("topix", "Date")):
        a0_5_inputs.market_pages(jq, receipt, dataset, column, now(), [])
    return result("recovered" if missing else "ready", available_at=receipt["completed_at"])


def jsf(project, day, now):
    path = project / "data/swing-forward/credit" / day / "manifest.json"
    acquired = "already_acquired" if path.exists() else swing_credit.capture(project, day)
    receipt = read(path)
    wait = (datetime.fromisoformat(receipt["available_at"]) - now()).total_seconds()
    if 0 < wait <= 1.1:
        sleep(wait)
    _, meta = swing_credit.read_credit(project, day, now())
    return result(
        "recovered" if acquired == "acquired" else "ready", available_at=meta["available_at"]
    )


def margins(project, day, now, root):
    empty_path = root / "margin-empty" / (day + ".json")
    manifest = project / "data/swing-forward/short-credit" / day / "manifest.json"
    if empty_path.is_file() and not manifest.exists():
        stamp = datetime.fromisoformat(read(empty_path)["observed_at"])
        current = now()
        due = evening_threshold(current.date().isoformat(), current)
        if (
            stamp.astimezone(JST).date() == current.date()
            and stamp <= current
            and (due is None or (stamp >= due and current - stamp < timedelta(minutes=15)))
        ):
            return result("empty", available_at=stamp.isoformat())
    state = shorts.capture_margin(project, day)
    if state == "empty":
        stamp = now().isoformat()
        save(empty_path, dict(date=day, observed_at=stamp))
        return result("empty", available_at=stamp)
    if state not in ("acquired", "already_acquired"):
        return result("stopped" if state == "stopped" else "error", reason=state)
    _, meta = shorts.margin_rows(project, day, None)
    wait = (datetime.fromisoformat(meta["available_at"]) - now()).total_seconds()
    if 0 < wait <= 1.1:
        sleep(wait)
    if datetime.fromisoformat(meta["available_at"]) > now():
        raise ValueError("margin_not_yet_available")
    return result(
        "ready" if state == "already_acquired" else "recovered", available_at=meta["available_at"]
    )


def schedule_input(project, target, now):
    root = project / "data/swing-forward"
    current = now()
    threshold = evening_threshold(target, current) or at(target, time(15, 30))

    def verified(cutoff):
        meta, events = earnings.asof(root, cutoff)
        if meta["state"] == "verified" and datetime.fromisoformat(meta["observed_at"]) >= threshold:
            return dict(
                available_at=meta["available_at"],
                manifest=meta["manifest"],
                companies=len(events),
                files=len(meta["sources"]),
            )
        return None

    for cutoff in (current, min(current, at(target, time.max))):
        record = verified(cutoff)
        if record is not None:
            return result("ready", **record)
    state = earnings.capture(root)["state"]
    if state != "captured":
        return result("stopped" if state == "stopped" else "error", reason=state)
    record = verified(now())
    if record is None:
        raise ValueError("earnings_receipt_unverified")
    return result("recovered", **record)


def ro(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True))


def flow(project, target, now):
    def checked():
        path = project / "data/a0-5-inputs/history.sqlite"
        if not path.is_file():
            return None
        endpoint = a0_5_inputs.SCHEMA["investors"][0]
        current = now()
        lo = (current - timedelta(days=42)).strftime("%Y%m")
        hi = current.strftime("%Y%m")
        with ro(path) as db:
            row = db.execute(
                "SELECT observed_at,http_status,file_count FROM discoveries "
                "WHERE endpoint=? ORDER BY observed_at DESC LIMIT 1",
                (endpoint,),
            ).fetchone()
            if not row or row[1] != 200 or not row[2]:
                return None
            stamp = datetime.fromisoformat(row[0])
            if not at(target, time(15, 30)) <= stamp <= current:
                return None
            items = db.execute(
                "SELECT object_key,state FROM bulk_files WHERE endpoint=?", (endpoint,)
            ).fetchall()
            active = [s for key, s in items if lo <= key.rsplit("_", 1)[-1][:6] <= hi]
            return row[0] if active and all(s == "complete" for s in active) else None

    stamp = checked()
    if stamp:
        return result("ready", available_at=stamp)
    a0_5_inputs.refresh_flow(project, now())
    stamp = checked()
    if not stamp:
        raise ValueError("flow_refresh_unverified")
    return result("recovered", available_at=stamp)


def evening_b(project, target, now):
    if target != now().date().isoformat() or not a0_5.enabled(project):
        return result("skipped")
    active = next(
        (
            p
            for p in read(project / "config/forward-plans.json")["plans"]
            if p["stage"] == "active"
            and p.get("adapter") == "plan_b_forward"
            and p["start"] <= target
        ),
        None,
    )
    if not active:
        return result("skipped")
    cap = active["capital"]
    return native_account(project, target, now, cap)


def native_account(project, target, now, cap):
    s = a0_5.book.load(a0_5.root_for(project, cap), cap)
    old = s["plans"].get(target, {})
    if old.get("state") == "ready":
        return result("ready", capital=cap)
    actual = a0_5.run(project, target, now=now(), capital=cap)
    return result(
        "recovered"
        if actual["state"] == "ready"
        else "stopped"
        if actual["state"] == "stopped"
        else "error",
        reason=actual["state"],
        capital=cap,
    )


def public(project, target, now):
    root = project / "data/free-market/public"
    db_path = root / "public.sqlite"
    suffix = "/" + target.replace("-", "") + "_Stocks_Restricted_Nextday.csv"
    expected = {shorts.JSF + k + ".csv" for k in ("meigara", "zandaka", "seigenichiran")}
    expected.add(shorts.SHORT_PAGE + "index.html")

    def checked():
        if not db_path.is_file():
            return False
        threshold = evening_threshold(target, now())
        with ro(db_path) as db:
            run = db.execute(
                "SELECT run,url FROM observations WHERE run LIKE 'short-%' AND url LIKE ? "
                "AND state='complete' ORDER BY id DESC LIMIT 1",
                (shorts.SHORT_PAGE + "%" + suffix,),
            ).fetchone()
            if not run:
                return False
            rows = db.execute(
                "SELECT url,raw_path,sha256,observed_at,available_at FROM observations "
                "WHERE run=? AND state='complete'",
                (run[0],),
            ).fetchall()
        found = set()
        available = None
        for url, local, sha, sampled, stamp in rows:
            if threshold and datetime.fromisoformat(sampled) < threshold:
                return False
            p = (root / local).resolve()
            if (
                not p.is_relative_to(root.resolve())
                or hashlib.sha256(p.read_bytes()).hexdigest() != sha
            ):
                raise ValueError("public_digest_mismatch")
            found.add(url)
            available = max(available, stamp) if available else stamp
        if not expected | {run[1]} <= found:
            return False
        wait = (datetime.fromisoformat(available) - now()).total_seconds()
        if 0 < wait <= 1.1:
            sleep(wait)
        if datetime.fromisoformat(available) > now():
            raise ValueError("public_not_yet_available")
        return available

    stamp = checked()
    if stamp:
        return result("ready", available_at=stamp)
    state = shorts.capture_public(project, target)
    if state == "acquired":
        stamp = checked()
        if not stamp:
            raise ValueError("public_receipt_unverified")
        return result("recovered", available_at=stamp)
    return result("stopped" if state == "stopped" else "error", reason=state)


def _record(root, report):
    save(root / "latest.json", report)
    if report.get("target"):
        save(root / "sessions" / (report["target"] + ".json"), report)


def _step(root, report, key, now, action):
    report["steps"][key] = result("running", date=report["target"])
    report["checked_at"] = now().isoformat()
    _record(root, report)
    try:
        step = action()
    except Exception as exc:
        step = result("error", reason=explain(exc))
    step["date"] = report["target"]
    report["steps"][key] = step
    report["checked_at"] = now().isoformat()
    _record(root, report)


def _end(root, report, now):
    report["status"] = (
        "ok" if all(r["status"] in GOOD for r in report["steps"].values()) else "attention"
    )
    report["checked_at"] = now().isoformat()
    _record(root, report)
    return report


def preopen_result(project, target, next_session, response, checked):
    if response.get("status") == "busy":
        return result("busy", reason="paper_busy")
    row = response.get("plans", {}).get("A")
    if row is None:
        configured = read(project / "config/forward-plans.json")["plans"]
        if all(p["id"] != "A" or p["stage"] != "active" for p in configured):
            return result("skipped", reason="plan_not_active")
        return result("error", reason="missing_plan_a_result")
    if target < row["start"]:
        return result("skipped", reason="plan_before_start")
    if row.get("status") == "error":
        return result("error", reason="plan_a_error")
    decision = row.get("decision_status")
    deadline = at(next_session, time(9))
    if row.get("next_entry") != next_session or row.get("last_decision") != target:
        return result("pending" if checked < deadline else "missed", reason="plan_a_pending")
    if decision in ("stopped", "disabled", "stopped_preopen"):
        return result("stopped", reason="plan_a_stopped")
    if decision != "ready":
        state = "missed" if decision == "missed_preopen" or checked >= deadline else "pending"
        return result(state, reason="plan_a_pending")
    recorded = datetime.fromisoformat(row.get("decision_recorded_at") or "")
    if recorded.utcoffset() is None or recorded > checked:
        return result("error", reason="invalid_decision_time")
    if recorded >= deadline:
        return result("missed", reason="decision_after_deadline")
    return result("ready", decision_recorded_at=recorded.isoformat())


def recover(project, *, now=None, on_a_inputs=None):
    project = Path(project).resolve()
    tick = clock(now)
    root = project / "data/plan-forward/recovery"
    report = dict(
        status="running",
        started_at=tick().isoformat(),
        checked_at=tick().isoformat(),
        target=None,
        next_session=None,
        steps={},
    )
    try:
        with single_writer(root):
            _record(root, report)
            try:
                target, next_day, prices, pubs = coverage(project, tick(), root)
            except Exception as exc:
                report.update(status="error", reason=explain(exc))
                _record(root, report)
                return report
            report.update(target=target, next_session=next_day)
            jobs = {
                "daily": lambda: grouped(prices, lambda d: daily(project, d, tick)),
                "jsf": lambda: grouped(pubs[-2:], lambda d: jsf(project, d, tick)),
                "earnings": lambda: schedule_input(project, target, tick),
                "margin": lambda: grouped(pubs, lambda d: margins(project, d, tick, root)),
                "flow": lambda: flow(project, target, tick),
                "native_evening": lambda: evening_b(project, target, tick),
            }
            for key, job in jobs.items():
                _step(root, report, key, tick, job)
                if key == "jsf" and on_a_inputs is not None:
                    current = tick()
                    if (
                        next_day == current.date().isoformat()
                        and current.time() < time(9)
                        and all(report["steps"][k]["status"] in GOOD for k in ("daily", "jsf"))
                    ):
                        # The A signal has a deadline. Independent B sources may still be slow.
                        # The caller advances the paper book with the live clock under its own lock.
                        def advance_a():
                            response = on_a_inputs(target, next_day)
                            return preopen_result(project, target, next_day, response, tick())

                        _step(root, report, "a_preopen", tick, advance_a)
            if all(report["steps"][k]["status"] in GOOD for k in ("daily", "margin")):
                save(root / "progress.json", dict(through=target))
            return _end(root, report, tick)
    except ValueError as exc:
        if str(exc) == "broker_state_already_open":
            return result("busy")
        raise


def recover_supplemental(project, target, *, now=None):
    """Finish native paper accounts, the intraday panel and public inputs after A/B decisions."""
    project = Path(project).resolve()
    root = project / "data/plan-forward/recovery"
    tick = clock(now)
    try:
        with single_writer(root):
            report = read(root / "latest.json")
            if report["target"] != target:
                raise ValueError("recovery_target_changed")
            # A resumed receipt from before retirement can still contain old account steps.
            report["steps"].pop("legacy_close", None)
            report["steps"].pop("review", None)
            report["status"] = "running"

            def native_positions():
                if not a0_5.enabled(project) or target > tick().date().isoformat():
                    return result("skipped")
                if target == tick().date().isoformat():
                    # The shared 22:00 workflow advances the remaining native books as well as B.
                    handled = report["steps"].get("native_evening", {}).get("capital")
                    accounts = {
                        cap: native_account(project, target, tick, cap)
                        for cap in a0_5.A0_5_PAPER_CAPITALS
                        if cap != handled
                    }
                    reason = next((v for v in accounts.values() if v["status"] not in GOOD), None)
                    state = (
                        "recovered"
                        if any(v["status"] == "recovered" for v in accounts.values())
                        else "ready"
                    )
                    return dict(reason or result(state), accounts=accounts)
                for cap in a0_5.A0_5_PAPER_CAPITALS:
                    state = a0_5.run(project, target, now=tick(), capital=cap)
                    if state["state"] != "reconciled":
                        return result(
                            "stopped" if state["state"] == "stopped" else "error",
                            reason=state["state"],
                        )
                return result("ready")

            def panel():
                panel_root = project / "data/rss-panel"
                policy = project / "config/daytrade-universe.json"
                if not policy.is_file() or target < read(policy)["first_decision"]:
                    return result("skipped")
                existing = (panel_root / "daily" / report["next_session"]).exists()
                if not existing and tick() >= at(report["next_session"], time(9)):
                    return result("missed", reason="panel_deadline_passed")
                prepare_panel(
                    project / "data/jquants", panel_root, date.fromisoformat(target), now=tick()
                )
                return result("ready" if existing else "recovered")

            for key, operation in (
                ("intraday_panel", panel),
                ("native_positions", native_positions),
                ("short_public", lambda: public(project, target, tick)),
            ):
                _step(root, report, key, tick, operation)
            return _end(root, report, tick)
    except ValueError as exc:
        if str(exc) == "broker_state_already_open":
            return result("busy")
        raise
