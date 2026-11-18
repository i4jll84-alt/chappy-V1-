"""Read only V1 versions available at the decision cutoff. No brokerage clients."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

import duckdb
import pandas as pd
import pyarrow.parquet as pq

from . import swing_earnings
from .a0_5_rules.config import A0_5, FINANCIAL_HISTORY_DAYS
from .a0_5_rules.data_fetcher import NUMERIC, SCHEMA, iso
from .a0_5_rules.data_processor import features
from .a0_5_rules.live_screen import freshness, latest_close
from .daytrade_universe import dated_inputs
from .swing_short_sources import margin_rows


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked(root, rel, digest):
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()) or sha(path) != digest:
        raise ValueError(f"入力原本の検証失敗 {rel}")
    return path


def bulk(root, endpoint, columns, lo, hi, cutoff, refs):
    if not (root / "history.sqlite").is_file():
        return []
    with closing(
        sqlite3.connect((root / "history.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        parts = db.execute(
            "SELECT object_key,modified,raw_path,sha256,parquet_path,parquet_sha256,"
            "observed_at,available_at "
            "FROM bulk_files WHERE endpoint=? AND state='complete' AND min_date<=? AND max_date>=? "
            "AND julianday(available_at)<=julianday(?) ORDER BY modified,available_at,object_key",
            (endpoint, hi, lo, cutoff.isoformat()),
        ).fetchall()
    latest = {r[0]: r for r in parts}
    frames = []
    for key, modified, raw, raw_hash, parquet, part_hash, observed, available in sorted(
        latest.values(), key=lambda r: (r[1], r[-1], r[0])
    ):
        checked(root, raw, raw_hash)
        path = checked(root, parquet, part_hash)
        names = pq.read_schema(path).names
        missing = set(columns) - set(names)
        if missing:
            raise ValueError(f"{endpoint} {lo}〜{hi} 欠損列 {sorted(missing)}")
        frame = pd.read_parquet(path, columns=columns)
        frame = frame.loc[frame[columns[0]].between(lo, hi)]
        frames.append(frame)
        refs.append(
            dict(
                endpoint=endpoint,
                key=key,
                modified=modified,
                observed_at=observed,
                available_at=available,
                sha256=raw_hash,
                parquet_sha256=part_hash,
            )
        )
    return frames


def calendar(project, day, cutoff, refs):
    fields = ["Date", "HolDiv"]
    lo = iso(day - pd.Timedelta(days=FINANCIAL_HISTORY_DAYS))
    hi = iso(day + pd.Timedelta(days=21))
    parts = bulk(
        project / "data/jquants/history", "/markets/calendar", fields, lo, hi, cutoff, refs
    )
    if not parts:
        raise ValueError(f"東証カレンダー {lo}〜{hi} 未受領")
    table = pd.concat(parts).drop_duplicates("Date", keep="last").set_index("Date")
    if (
        not set(pd.date_range(lo, hi).strftime("%Y-%m-%d")) <= set(table.index)
        or not table.HolDiv.isin(["0", "1", "2", "3"]).all()
    ):
        raise ValueError(f"東証カレンダー {lo}〜{hi} 不完全")
    return pd.DatetimeIndex(sorted(table.index[table.HolDiv.isin(["1", "2"])]))


def market_pages(root, receipt, name, date_col, cutoff, refs):
    job = receipt["jobs"][name]
    folder = root / "market_financial" / name
    with duckdb.connect(str(folder / "catalog.duckdb"), read_only=True) as db:
        pages = db.execute(
            "SELECT raw_path,raw_sha256,observed_at,row_count FROM pages "
            "WHERE job_id=? ORDER BY page_number",
            [job["job_id"]],
        ).fetchall()
        stored = db.execute(
            "SELECT params_json,state,pages,rows_received,completed_at FROM jobs WHERE job_id=?",
            [job["job_id"]],
        ).fetchone()
    day = receipt["date"]
    expected = {"date": day} if name == "financials" else {"from": day, "to": day}
    if (
        not stored
        or json.loads(stored[0]) != expected
        or stored[1] != job["state"]
        or stored[2] != len(pages)
        or stored[3] != job["rows_received"]
        or not stored[4]
        or stored[4] > cutoff
        or not pages
    ):
        raise ValueError(f"{name} {day} 未確定ジョブ")
    rows = []
    for rel, digest, observed, count in pages:
        page = json.loads(checked(folder, rel, digest).read_bytes())["data"]
        if (
            observed + timedelta(seconds=1) > cutoff
            or len(page) != count
            or any(r[date_col] != day for r in page)
        ):
            raise ValueError(f"{name} {day} 受領時刻・件数不一致")
        rows.extend(page)
        refs.append(
            dict(
                dataset=name,
                date=day,
                sha256=digest,
                job=job["job_id"],
                observed_at=observed.isoformat(),
                completed_at=stored[4].isoformat(),
            )
        )
    if len(rows) != job["rows_received"] or not (
        job["state"] == "complete" or (name == "financials" and job["state"] == "empty")
    ):
        raise ValueError(f"{name} {day} 未確定")
    return rows


KEYS = {
    "bars": ["Date", "Code"],
    "master": ["Date", "Code"],
    "topix": ["Date"],
    "financial": ["DiscDate", "Code", "DiscNo"],
    "investors": ["PubDate", "EnDate", "Section"],
    "margin": ["Date", "Code", "PubDate"],
}


def normalize(name, frames):
    cols = SCHEMA[name][1].split()
    if not frames:
        return pd.DataFrame(columns=cols)
    for frame in frames:
        if missing := set(cols) - set(frame):
            raise ValueError(f"{name} 欠損列 {sorted(missing)}")
        if frame.duplicated(KEYS[name]).any():
            raise ValueError(f"{name} 同一原本で主キーが重複")
    frame = pd.concat([f[cols] for f in frames], ignore_index=True).replace("", pd.NA)
    for col in cols:
        if col in NUMERIC:
            frame[col] = pd.to_numeric(frame[col], errors="raise").astype(float)
        elif col in ("Date", "DiscDate", "CurPerSt", "CurPerEn", "PubDate", "EnDate"):
            frame[col] = pd.to_datetime(frame[col], format="ISO8601").astype("datetime64[ns]")
        else:
            frame[col] = frame[col].astype("string")
    return frame.drop_duplicates(KEYS[name], keep="last")


def read_inputs(project, day, cutoff):
    root = project / "data/jquants"
    refs = []
    cal = calendar(project, day, cutoff, refs)
    if day != latest_close(cal, cutoff):
        raise ValueError(f"判定日 {iso(day)} は直近確定立会日と不一致")
    i = cal.get_loc(day)
    if i < 28 or len(cal) <= i + 3:
        raise ValueError("必要な過去・翌3立会日のカレンダー不足")
    sessions = cal[i - 28 : i + 1]
    raw = {}
    for name, (endpoint, fields) in SCHEMA.items():
        lo = (
            iso(day - pd.Timedelta(days=FINANCIAL_HISTORY_DAYS))
            if name == "financial"
            else iso(sessions[0])
        )
        raw[name] = bulk(root / "history", endpoint, fields.split(), lo, iso(day), cutoff, refs)
        if name == "investors":
            raw[name] += bulk(
                project / "data/a0-5-inputs", endpoint, fields.split(), lo, iso(day), cutoff, refs
            )
    last_bulk_filing = max(
        (str(f.DiscDate.max())[:10] for f in raw["financial"] if not f.empty), default=""
    )
    daily_filings = set()
    for session in sessions:
        date = iso(session)
        rec = root / "daily-snapshots" / date / "last_collection.json"
        if (
            not rec.is_file()
            or datetime.fromisoformat(json.loads(rec.read_bytes())["completed_at"]) > cutoff
        ):
            if date == iso(day):
                raise ValueError(f"日足受領票 {date} が締切までに未受領")
            continue
        cols, master, sources = dated_inputs(root, [date], cutoff)
        refs.extend(sources)
        receipt = json.loads(rec.read_bytes())
        if datetime.fromisoformat(receipt["completed_at"]) > cutoff:
            raise ValueError(f"日次受領票の版が更新されました {date}")
        raw["bars"].append(pd.DataFrame([r[0] for r in cols.values()]))
        raw["master"].append(pd.DataFrame(master.values()))
        for name, dataset, key in (
            ("financial", "financials", "DiscDate"),
            ("topix", "topix", "Date"),
        ):
            rows = market_pages(root, receipt, dataset, key, cutoff, refs)
            if rows:
                raw[name].append(pd.DataFrame(rows))
        daily_filings.add(date)
        refs.append(dict(date=date, daily_receipt=sha(rec), completed_at=receipt["completed_at"]))
    # Financial comparisons need prior-year filings, including receipts older than the price window.
    first_financial = iso(day - pd.Timedelta(days=FINANCIAL_HISTORY_DAYS))
    for rec in sorted((root / "daily-snapshots").glob("*/last_collection.json")):
        date = rec.parent.name
        if not first_financial <= date < iso(sessions[0]):
            continue
        receipt = json.loads(rec.read_bytes())
        if (
            receipt["state"] != "complete"
            or datetime.fromisoformat(receipt["completed_at"]) > cutoff
        ):
            continue
        daily_filings.add(date)
        rows = market_pages(root, receipt, "financials", "DiscDate", cutoff, refs)
        if rows:
            raw["financial"].append(pd.DataFrame(rows))
    for manifest in sorted((project / "data/swing-forward/short-credit").glob("*/manifest.json")):
        date = manifest.parent.name
        if iso(sessions[0]) <= date < iso(day):
            rows, meta = margin_rows(project, date, cutoff)
            if rows:
                raw["margin"].append(pd.DataFrame(rows.values()))
                refs.append(
                    dict(
                        dataset="margin",
                        published=date,
                        **{k: meta[k] for k in ("pages", "observed_at", "available_at")},
                    )
                )
    inputs = {k: normalize(k, v) for k, v in raw.items()}
    gaps = []
    missing_filings = [
        iso(d)
        for d in cal
        if max(first_financial, last_bulk_filing) < iso(d) <= iso(day)
        and iso(d) not in daily_filings
    ]
    if missing_filings:
        gaps.append("決算受領票の欠損日 " + ",".join(missing_filings))
    for name in ("bars", "master", "topix"):
        absent = sessions.difference(inputs[name].Date)
        if len(absent):
            gaps.append(name + " 欠損日 " + ",".join(map(iso, absent)))
    for name in ("financial", "investors", "margin"):
        if inputs[name].empty:
            gaps.append(f"{name} {iso(day)} 利用可能な原本なし")
    flow = inputs["investors"]
    valid = flow.loc[
        flow.PubDate.lt(day)
        & flow.Section.eq("TSEPrime" if day >= pd.Timestamp("2022-04-15") else "TSE1st")
    ]
    if valid.empty or (day - valid.EnDate.max()).days > 21:
        last = "なし" if valid.empty else iso(valid.EnDate.max())
        gaps.append(f"投資部門別 FrgnBal {iso(day)} 有効期限切れ（直近期間末 {last}）")
    if gaps:
        raise ValueError(" / ".join(gaps))
    for session in sessions:
        if set(inputs["bars"].loc[inputs["bars"].Date.eq(session), "Code"]) != set(
            inputs["master"].loc[inputs["master"].Date.eq(session), "Code"]
        ):
            raise ValueError(f"日足・マスターの原本コード不一致 {iso(session)}")
    table = features(inputs, sessions, A0_5)
    counts = freshness(table.loc[table.Date.isin(sessions[-6:])], day, cal, now=cutoff)
    today_table = table.loc[table.Date.eq(day)].copy()
    counts.update(
        daily_rows=len(today_table), market_universe=int(today_table.MarketUniverse.sum())
    )
    return today_table, cal, counts, refs


def schedules(project, codes, day, cutoff, manual):
    root = project / "data/swing-forward"
    meta, events = swing_earnings.asof(root, cutoff)
    preopen = day.date() < cutoff.date() and cutoff.hour < 9
    if preopen and meta.get("reason") == "no_snapshot_by_cutoff":
        end_of_signal_day = datetime.combine(day.date(), datetime.max.time(), cutoff.tzinfo)
        meta, events = swing_earnings.asof(root, end_of_signal_day)
    ref = {
        "source": meta.get("manifest"),
        "sha256": meta.get("manifest_sha256"),
        "checked_on": (meta.get("available_at") or iso(day))[:10],
        "available_at": meta.get("available_at"),
    }
    result = {}
    for code in codes:
        override = manual.get(iso(day), {}).get(code)
        morning = manual.get(cutoff.date().isoformat(), {}).get(code) if preopen else None
        if morning and datetime.fromisoformat(morning["received_at"]) <= cutoff:
            override = morning
        if override and datetime.fromisoformat(override["received_at"]) <= cutoff:
            result[code] = (override["scheduled_date"], "手動確認 " + override["source"])
            continue
        dates = {r[0] for r in events.get(code, [])}
        future = sorted(d for d in dates if d and d > iso(day))
        result[code] = (
            (future[0], "JPX " + str(ref["source"]))
            if future and None not in dates and len(future) == 1
            else (None, "予定未確認")
        )
    return result, ref


def refresh_flow(project, now):
    """Update a small private V1 bulk archive from the same official endpoint as the fixed input."""
    from trading_system.brokers.locking import single_writer
    from trading_system.history_download import Archive, HistoryClient
    from trading_system.settings import api_key

    root = project / "data/a0-5-inputs"
    if any(
        (project / p / "STOP").exists()
        for p in ("data/a0-5-paper", "data/swing-forward", "data/jquants/history")
    ):
        raise ValueError("ペーパー・取得停止中")
    secret = api_key()
    if secret is None:
        raise ValueError("J-Quantsのキーが未設定")
    with single_writer(root):
        db = Archive(root)
        client = HistoryClient(secret.get_secret_value(), root)
        try:
            endpoint = SCHEMA["investors"][0]
            db.discover(client, endpoint)
            check = db.db.execute(
                "SELECT http_status FROM discoveries WHERE endpoint=? "
                "ORDER BY observed_at DESC LIMIT 1",
                (endpoint,),
            ).fetchone()
            if not check or check[0] != 200:
                raise ValueError("投資部門別の提供一覧を確認できません")
            lo, hi = (
                (pd.Timestamp(now) - pd.Timedelta(days=42)).strftime("%Y%m"),
                pd.Timestamp(now).strftime("%Y%m"),
            )
            for item in db.db.execute(
                "SELECT * FROM bulk_files WHERE endpoint=? AND state!='complete' ORDER BY modified",
                (endpoint,),
            ).fetchall():
                tag = item["object_key"].rsplit("_", 1)[-1][:6]
                if lo <= tag <= hi:
                    db.fetch_bulk(client, item)
        finally:
            client.close()
            db.close()
