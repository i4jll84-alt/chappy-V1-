"""Freeze tomorrow's common research universe from verified end-of-day observations."""

import csv
import hashlib
import json
import math
import sqlite3
import subprocess
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import duckdb

from trading_system.brokers.locking import single_writer
from trading_system.research.swing_windows import history_rows
from trading_system.research_panel import PROJECT, configuration, panel_symbols
from trading_system.storage.store import canonical

JST = ZoneInfo("Asia/Tokyo")
CONTROL = "72030"
RULE = "five_sessions_turnover100_range30_v1"


def panel_location(root, day):
    """Return the published panel for this session; legacy mode ends on explicit adoption."""
    folder = root / "daily" / day.isoformat()
    if folder.exists():
        manifest = json.loads((folder / "selection.json").read_bytes())
        selected_at = datetime.fromisoformat(manifest["selected_at"])
        published = datetime.fromisoformat(manifest["published_at"])
        if (
            manifest.get("preview")
            or manifest["target_session"] != day.isoformat()
            or not manifest["source_date"] < day.isoformat()
            or published < selected_at
            or published >= datetime.combine(day, time(9), JST)
        ):
            raise ValueError("invalid_panel_configuration")
        config = json.loads((folder / "panel.json").read_bytes())
        symbols = panel_symbols(config)
        if config.get("session") != day.isoformat() or symbols != [
            CONTROL[:4] + ".T",
            *[x["code"][:4] + ".T" for x in manifest["selected"]],
        ]:
            raise ValueError("invalid_panel_configuration")
        return folder
    policy = root.parent.parent / "config/daytrade-universe.json"
    if policy.exists() and day >= date.fromisoformat(
        json.loads(policy.read_bytes())["from_session"]
    ):
        raise ValueError("daily_panel_missing")
    return root


def calendar_days(root, day, asof):
    archive = root / "history"
    path = (archive / "history.sqlite").resolve()
    if (archive / "history.sqlite-wal").exists() and (
        archive / "history.sqlite-wal"
    ).stat().st_size:
        raise ValueError("calendar_archive_busy")
    evidence = []
    lo = day - timedelta(days=30)
    hi = day + timedelta(days=20)
    days = {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        for r in history_rows(
            db, archive, "/markets/calendar", ["Date", "HolDiv"], str(lo), str(hi), evidence
        ):
            if (
                r["HolDiv"] not in ("0", "1", "2", "3")
                or days.setdefault(r["Date"], r["HolDiv"]) != r["HolDiv"]
            ):
                raise ValueError("invalid_exchange_calendar")
    if not evidence or any(datetime.fromisoformat(x["available_at"]) > asof for x in evidence):
        raise ValueError("calendar_not_yet_available")
    return days, evidence


def latest_closed_session(root, now):
    local = now.astimezone(JST)
    day = local.date()
    days, _ = calendar_days(root, day, now)
    if local.time() < time(16, 30):
        day -= timedelta(days=1)
    for _ in range(20):
        if days[str(day)] in ("1", "2"):
            return day
        day -= timedelta(days=1)
    raise ValueError("no_recent_trading_session")


def window(root, day, asof):
    days, evidence = calendar_days(root, day, asof)
    sessions = sorted(d for d, flag in days.items() if flag in ("1", "2"))
    if str(day) not in sessions:
        raise ValueError("decision_date_not_a_session")
    idx = sessions.index(str(day))
    if idx < 4 or idx + 1 >= len(sessions):
        raise ValueError("incomplete_exchange_calendar")
    selected = sessions[idx - 4 : idx + 1]
    next_day = date.fromisoformat(sessions[idx + 1])
    start = date.fromisoformat(selected[0])
    if any(str(start + timedelta(days=i)) not in days for i in range((next_day - start).days + 1)):
        raise ValueError("incomplete_exchange_calendar")
    return selected, next_day, evidence


def dated_inputs(root, days, asof):
    from trading_system.research.swing_daily import verify_pages

    columns = defaultdict(list)
    sources = []
    for day in days:
        base = (root / "daily-snapshots" / day).resolve()
        receipt = json.loads((base / "last_collection.json").read_bytes())
        manifest = Path(receipt["daily_manifest"]).resolve()
        if (
            receipt["date"] != day
            or receipt["state"] != "complete"
            or not manifest.is_relative_to(base)
            or datetime.fromisoformat(receipt["completed_at"]) > asof
        ):
            raise ValueError("invalid_day_receipt")
        meta = json.loads(manifest.read_bytes())
        count, _ = verify_pages(manifest.parent, meta, day)
        if meta["state"] != "verified" or count != meta["rows"] or count != receipt["daily_rows"]:
            raise ValueError("unverified_daily_prices")
        for page in meta["pages"]:
            if datetime.fromisoformat(page["available_at"]) > asof:
                raise ValueError("prices_not_yet_available")
            body = (manifest.parent / page["file"]).read_bytes()
            if hashlib.sha256(body).hexdigest() != page["sha256"]:
                raise ValueError("daily_hash_mismatch")
            for row in json.loads(body)["data"]:
                columns[row["Code"]].append(row)
        sources.append(
            dict(
                date=day,
                manifest=str(manifest.relative_to(root)),
                sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
                completed_at=receipt["completed_at"],
            )
        )
    master_dir = root / "reference/master"
    job = receipt["jobs"]["master"]
    if job["state"] != "complete":
        raise ValueError("invalid_master")
    master = {}
    with duckdb.connect(str(master_dir / "catalog.duckdb"), read_only=True) as db:
        for rel, digest, observed in db.execute(
            "SELECT raw_path,raw_sha256,observed_at FROM pages WHERE job_id=? ORDER BY page_number",
            [job["job_id"]],
        ).fetchall():
            path = (master_dir / rel).resolve()
            if not path.is_relative_to(master_dir) or observed + timedelta(seconds=1) > asof:
                raise ValueError("invalid_master_page")
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != digest:
                raise ValueError("master_hash_mismatch")
            for row in json.loads(body)["data"]:
                if row["Date"] != days[-1] or row["Code"] in master:
                    raise ValueError("invalid_master_row")
                master[row["Code"]] = row
            sources.append(dict(master_job=job["job_id"], page=rel, sha256=digest))
    if len(master) != job["rows_received"] or set(master) != {
        k for k, v in columns.items() if v[-1]["Date"] == days[-1]
    }:
        raise ValueError("master_price_coverage_mismatch")
    return columns, master, sources


def rank(columns, master, days):
    counts = Counter()
    pool = []
    for code, info in master.items():
        if code == CONTROL:
            counts["control"] += 1
            continue
        if (
            info["Mkt"] not in ("0111", "0112", "0113")
            or info["ProdCat"] != "011"
            or not code.endswith("0")
        ):
            counts["outside_scope"] += 1
            continue
        rows = columns.get(code, [])
        if [r["Date"] for r in rows] != days or any(
            not all(
                isinstance(r.get(k), (int, float)) and math.isfinite(r[k]) and r[k] > 0
                for k in ("O", "H", "L", "C", "Vo", "Va", "AdjFactor")
            )
            or not r["L"] <= min(r["O"], r["C"]) <= max(r["O"], r["C"]) <= r["H"]
            for r in rows
        ):
            counts["missing_price_or_turnover"] += 1
            continue
        if any(r["AdjFactor"] != 1 or r.get("ExRT") not in (None, "") for r in rows):
            counts["corporate_action"] += 1
            continue
        if rows[-1]["C"] * 100 * 1.0005 + 10 > 1000000:
            counts["over_budget_at_close"] += 1
            continue
        pool.append(
            dict(
                code=code,
                name=info["CoName"],
                market=info["MktNm"],
                sector=info["S33Nm"],
                close=rows[-1]["C"],
                turnover_median=median(r["Va"] for r in rows),
                range_median=median((r["H"] - r["L"]) / r["C"] for r in rows),
            )
        )
    if CONTROL not in master or len(pool) < 30:
        raise ValueError("insufficient_daily_candidates")
    liquid = sorted(pool, key=lambda x: (-x["turnover_median"], x["code"]))[:100]
    ranked = sorted(liquid, key=lambda x: (-x["range_median"], -x["turnover_median"], x["code"]))
    return ranked[:30], dict(counts, eligible=len(pool), liquidity_shortlist=len(liquid))


def build_book(folder):
    runtime = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
    builder = PROJECT / "scripts/build_research_panel.mjs"
    subprocess.run(
        [
            str(runtime / "bin/node.exe"),
            str(builder),
            str(folder / "selection.json"),
            str(folder),
            "--runtime-packages",
            str(runtime / "node_modules"),
        ],
        cwd=folder,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=True,
        timeout=180,
        capture_output=True,
    )
    return configuration(folder)


def prepare(root, panel_root, day, *, now=None, preview=False):
    root = root.resolve()
    asof = now or datetime.now(UTC)
    if asof < datetime.combine(day, time(16, 30), JST):
        raise ValueError("daily_data_after_1630_only")
    if preview and (panel_root.parent.parent / "config/daytrade-universe.json").is_file():
        raise ValueError("preview_requires_separate_output")
    days, target, calendar = window(root, day, asof)
    base = panel_root / "daily"
    target_dir = base / str(target)
    with single_writer(base):
        if target_dir.exists():
            panel_location(panel_root, target)
            configuration(target_dir)
            return target_dir
        if asof >= datetime.combine(target, time(9), JST) and not preview:
            raise ValueError("panel_deadline_passed")
        columns, master, sources = dated_inputs(root, days, asof)
        selected, counts = rank(columns, master, days)
        with TemporaryDirectory(dir=base) as tmp:
            folder = Path(tmp) / str(target)
            folder.mkdir()
            manifest = dict(
                rule=RULE,
                source_date=str(day),
                target_session=str(target),
                selected_at=asof.isoformat(),
                control=CONTROL,
                selected=selected,
                lookback=days,
                counts=counts,
                calendar=calendar,
                sources=sources,
                preview=preview,
            )
            (folder / "selection.json").write_text(canonical(manifest), encoding="utf-8")
            with (folder / "候補.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "順位",
                        "銘柄コード",
                        "銘柄名",
                        "市場",
                        "業種",
                        "終値",
                        "5日売買代金中央値（円）",
                        "5日日中値幅率中央値（%）",
                    ]
                )
                for i, r in enumerate(selected, 1):
                    writer.writerow(
                        [
                            i,
                            r["code"][:4],
                            r["name"],
                            r["market"],
                            r["sector"],
                            r["close"],
                            r["turnover_median"],
                            round(100 * r["range_median"], 4),
                        ]
                    )
            symbols, _ = build_book(folder)
            if symbols != [CONTROL[:4] + ".T", *[r["code"][:4] + ".T" for r in selected]]:
                raise ValueError("generated_panel_mismatch")
            manifest["published_at"] = (now or datetime.now(UTC)).isoformat()
            if not preview and datetime.fromisoformat(manifest["published_at"]) >= datetime.combine(
                target, time(9), JST
            ):
                raise ValueError("panel_deadline_passed")
            (folder / "selection.json").write_text(canonical(manifest), encoding="utf-8")
            folder.rename(target_dir)
        return target_dir
