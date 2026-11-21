"""Read-only, as-of sources for Plan C. Daily receipts supersede archived bulk rows."""

import json
import math
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from . import a0_5_inputs as saved
from . import c1_signals, swing_earnings
from .a0_5_rules.data_fetcher import SCHEMA, iso
from .a0_5_rules.live_screen import latest_close
from .daytrade_universe import JST, dated_inputs
from .swing_short_sources import margin_rows

FIELDS = {
    "bars": "Date Code O H L C Vo Va AdjFactor ExRT",
    "master": "Date Code Mkt S33 ProdCat",
    "topix": "Date C",
    "margin": "Date PubDate Code LongVol",
}
SOURCE_ERRORS = (OSError, KeyError, ValueError, TypeError, duckdb.Error, sqlite3.Error)


def _stamp(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("timezone_required")
    return stamp.to_pydatetime()


def _bulk(project, name, start, end, cutoff, refs):
    info = []
    frames = saved.bulk(
        project / "data/jquants/history",
        SCHEMA[name][0],
        FIELDS[name].split(),
        start,
        end,
        cutoff,
        info,
    )
    for frame, ref in zip(frames, info, strict=True):
        if not _stamp(ref["observed_at"]) <= _stamp(ref["available_at"]) <= cutoff:
            raise ValueError("invalid_bulk_receipt")
        if name == "margin":
            frame["AvailableDay"] = min(iso(_stamp(ref["available_at"]).astimezone(JST)), end)
    refs.extend(info)
    return frames


def _combined(name, frames):
    keys = {
        "bars": ["Date", "Code"],
        "master": ["Date", "Code"],
        "topix": ["Date"],
        "margin": ["Date", "Code", "PubDate"],
    }[name]
    if not frames:
        return pd.DataFrame(columns=FIELDS[name].split())
    if any(frame.duplicated(keys).any() for frame in frames):
        raise ValueError(f"duplicate_{name}_row")
    joined = pd.concat(frames, ignore_index=True)
    if name == "margin" and "AvailableDay" in joined:
        keys = [*keys, "AvailableDay"]
    return joined.drop_duplicates(keys, keep="last")


def _with_daily(archives, snapshots):
    # A complete newer cross-section can remove a code as well as change its values.
    replaced = {frame.Date.iloc[0] for frame in snapshots}
    return [frame.loc[~frame.Date.isin(replaced)] for frame in archives] + snapshots


def _receipt(root, day, cutoff):
    path = root / "daily-snapshots" / day / "last_collection.json"
    if not path.is_file():
        return None
    receipt = json.loads(path.read_bytes())
    if _stamp(receipt["completed_at"]) > cutoff:
        return None
    if receipt["date"] != day or receipt["state"] != "complete":
        raise ValueError("incomplete_daily_receipt")
    return receipt


def _earnings(project, day, cutoff, refs):
    root = project / "data/swing-forward"
    meta, events = swing_earnings.asof(root, cutoff)
    if meta.get("reason") == "no_snapshot_by_cutoff" and day < cutoff.date().isoformat():
        prev = datetime.combine(pd.Timestamp(day).date(), time.max, JST)
        meta, events = swing_earnings.asof(root, prev)
    earnings = {}
    if meta.get("state") == "verified":
        refs.append({k: meta[k] for k in ("manifest", "manifest_sha256", "available_at")})
        for code, rows in events.items():
            dates = {row[0] for row in rows}
            if None in dates:
                continue
            future = sorted(d for d in dates if d >= day)
            if future:
                earnings[code] = future
    # Absence from the JPX announcement list is not proof of no earnings.
    # An explicit, dated negative declaration can be supplied with its source.
    path = project / "data/c1-inputs/earnings.json"
    if path.is_file():
        docs = json.loads(path.read_bytes())
        applicable = [
            row
            for row in docs["observations"]
            if row["signal_date"] == day
            and _stamp(row["published_at"]) <= _stamp(row["available_at"]) <= cutoff
            and row.get("source_ref")
        ]
        grouped = {}
        for row in applicable:
            grouped.setdefault(row["code"], []).append(row)
        for code, versions in grouped.items():
            last = max(_stamp(row["available_at"]) for row in versions)
            latest = [row for row in versions if _stamp(row["available_at"]) == last]
            variants = {json.dumps(row["dates"], sort_keys=True) for row in latest}
            dates = latest[0]["dates"]
            if len(variants) != 1 or not isinstance(dates, list):
                earnings.pop(code, None)
                continue
            if any(iso(d) != d or d < day for d in dates):
                raise ValueError("invalid_earnings_evidence")
            earnings[code] = sorted(set(dates))
        if applicable:
            refs.append(dict(dataset="c1_earnings", sha256=saved.sha(path)))
    return earnings


def _latest(rows, source, day, cutoff):
    usable = [
        row
        for row in rows
        if row.get("source") == source
        and row.get("date") == day
        and row.get("source_ref")
        and _stamp(row["available_at"]) <= cutoff
    ]
    if not usable:
        return None
    stamp = max(_stamp(row["available_at"]) for row in usable)
    latest = [row for row in usable if _stamp(row["available_at"]) == stamp]
    return latest[0] if all(row == latest[0] for row in latest) else None


def _volatility(document, source, day, sessions, cutoff, refs):
    if source == "NIKKEI_VI":
        earlier = [s for s in sessions if s < day]
        if not earlier:
            return None
        previous = earlier[-1]
    elif source == "VIX":
        prior = pd.Timestamp(day).date() - timedelta(days=1)
        while True:
            row = _latest(document.get("calendar", []), source, prior.isoformat(), cutoff)
            if not row or not isinstance(row.get("is_session"), bool):
                return None
            refs.append(dict(dataset="volatility_calendar", **row))
            if row["is_session"]:
                previous = prior.isoformat()
                break
            prior -= timedelta(days=1)
    else:
        raise ValueError("invalid_volatility_source")
    final = [
        row
        for row in document.get("observations", [])
        if row.get("source") == source
        and row.get("date") == previous
        and row.get("final") is True
        and _stamp(row["published_at"]) <= _stamp(row["available_at"]) <= cutoff
    ]
    row = _latest(final, source, previous, cutoff)
    if not row:
        return None
    value = float(row["close"])
    if not math.isfinite(value) or value < 0:
        return None
    refs.append(dict(dataset="volatility", **row))
    return value


def load_bars(project, date, cutoff):
    """Return verified EOD OHLC. A pre-close call waits for the official daily receipt."""
    project, day, cutoff = Path(project), iso(date), _stamp(cutoff).astimezone(JST)
    if cutoff < datetime.combine(pd.Timestamp(day).date(), time(15, 30), JST):
        raise ValueError("official_daily_bars_not_available")
    root = project / "data/jquants"
    if _receipt(root, day, cutoff) is not None:
        columns, _, _ = dated_inputs(root, [day], cutoff)
        if any(len(series) != 1 for series in columns.values()):
            raise ValueError("duplicate_official_daily_bar")
        rows = [r[0] for r in columns.values()]
    else:
        # Preserve rights flags for the execution engine on archived bars as well.
        fields = FIELDS["bars"].split()
        refs = []
        frames = saved.bulk(root / "history", SCHEMA["bars"][0], fields, day, day, cutoff, refs)
        if any(not _stamp(r["observed_at"]) <= _stamp(r["available_at"]) <= cutoff for r in refs):
            raise ValueError("invalid_bulk_receipt")
        rows = _combined("bars", frames).to_dict("records")
    if not rows or any(row.get("Date") != day for row in rows):
        raise ValueError("official_daily_bars_missing")
    result = {}
    for row in rows:
        clean = {}
        for key, column in (
            ("open", "O"),
            ("high", "H"),
            ("low", "L"),
            ("close", "C"),
            ("adj_factor", "AdjFactor"),
        ):
            value = pd.to_numeric(row.get(column), errors="coerce")
            clean[key] = float(value) if pd.notna(value) and math.isfinite(value) else None
        ex = row.get("ExRT")
        clean["ex_rights"] = str(ex) if pd.notna(ex) and ex != "" else None
        volume = pd.to_numeric(row.get("Vo"), errors="coerce")
        clean["no_trade"] = bool(
            pd.notna(volume)
            and volume == 0
            and all(k in row and (pd.isna(row[k]) or row[k] == "") for k in ("O", "H", "L", "C"))
        )
        result[str(row["Code"])] = clean
    return result


def load_day(
    project,
    signal_date,
    cutoff,
    *,
    rsi_threshold=c1_signals.DEFAULT_RSI_THRESHOLD,
    enable_vol_circuit_breaker=False,
    vol_threshold=30,
    vol_source="NIKKEI_VI",
    volatility=None,
):
    """Load immutable-by-cutoff sources and return an entry gate, exit marks and evidence."""
    project, day, cutoff = Path(project), iso(signal_date), _stamp(cutoff).astimezone(JST)
    root = project / "data/jquants"
    if cutoff < datetime.combine(pd.Timestamp(day).date(), time(15, 30), JST):
        raise ValueError("signal_close_in_future")
    refs, missing = [], []
    try:
        calendar = saved.calendar(project, pd.Timestamp(day), cutoff, refs)
    except SOURCE_ERRORS as exc:
        calendar = pd.DatetimeIndex([day])
        missing.append(f"calendar_unavailable:{type(exc).__name__}")
    if not missing and pd.Timestamp(day) != latest_close(calendar, cutoff):
        raise ValueError("signal_is_not_latest_completed_session")
    days = [iso(d) for d in calendar if iso(d) <= day][-c1_signals.REQUIRED_SESSIONS :]
    all_days = [iso(d) for d in calendar]
    raw = {name: [] for name in FIELDS}
    for name in raw:
        try:
            raw[name] = _bulk(project, name, days[0], day, cutoff, refs)
        except SOURCE_ERRORS as exc:
            missing.append(f"{name}_archive_unavailable:{type(exc).__name__}")
    snapshots = {name: [] for name in ("bars", "master", "topix")}
    for session in days:
        try:
            receipt = _receipt(root, session, cutoff)
            if receipt is None:
                continue
            columns, master, evidence = dated_inputs(root, [session], cutoff)
            if not columns or not master:
                raise ValueError("empty_daily_cross_section")
            snapshots["bars"].append(pd.DataFrame([r[0] for r in columns.values()]))
            snapshots["master"].append(pd.DataFrame(master.values()))
            refs.extend(evidence)
        except SOURCE_ERRORS as exc:
            missing.append(f"daily_prices_unverified:{session}:{type(exc).__name__}")
            continue
        try:
            rows = saved.market_pages(root, receipt, "topix", "Date", cutoff, refs)
            if rows:
                snapshots["topix"].append(pd.DataFrame(rows))
        except SOURCE_ERRORS as exc:
            missing.append(f"topix_unverified:{session}:{type(exc).__name__}")
    for name, frames in snapshots.items():
        raw[name] = _with_daily(raw[name], frames)
    base = project / "data/swing-forward/short-credit"
    for manifest in sorted(base.glob("*/manifest.json")):
        pub = manifest.parent.name
        if not days[0] <= pub <= day:
            continue
        try:
            rows, meta = margin_rows(project, pub, cutoff)
            if rows:
                f = pd.DataFrame(rows.values())
                f["AvailableDay"] = min(iso(_stamp(meta["available_at"]).astimezone(JST)), day)
                raw["margin"].append(f)
                refs.append(
                    dict(
                        dataset="margin",
                        published=pub,
                        available_at=meta["available_at"],
                        sha256=saved.sha(manifest),
                    )
                )
        except SOURCE_ERRORS as exc:
            missing.append(f"margin_unverified:{pub}:{type(exc).__name__}")
    frames = {name: _combined(name, items) for name, items in raw.items()}
    try:
        earnings = _earnings(project, day, cutoff, refs)
    except SOURCE_ERRORS as exc:
        earnings = {}
        missing.append(f"earnings_unverified:{type(exc).__name__}")
    vol_value = None
    if enable_vol_circuit_breaker:
        try:
            if volatility is None:
                path = project / "data/c1-inputs/volatility.json"
                volatility = json.loads(path.read_bytes()) if path.is_file() else {}
            vol_value = _volatility(volatility, vol_source, day, all_days, cutoff, refs)
        except SOURCE_ERRORS as exc:
            missing.append(f"volatility_unverified:{type(exc).__name__}")
    result = c1_signals.evaluate(
        days,
        **frames,
        earnings=earnings,
        rsi_threshold=rsi_threshold,
        enable_vol_circuit_breaker=enable_vol_circuit_breaker,
        vol_threshold=vol_threshold,
        vol_value=vol_value,
    )
    if missing:
        result.update(coverage_ok=False, allow_entries=False, ranked=[])
        result["reasons"].extend(missing)
    return dict(result, sessions=all_days, refs=refs)
