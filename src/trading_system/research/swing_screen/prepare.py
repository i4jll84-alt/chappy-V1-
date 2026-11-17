"""Build auditable retrospective blocks or freeze a daily candidate file."""

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from ...brokers.locking import single_writer
from ...storage.store import atomic_write
from .. import swing_earnings
from .features import Features, annotate, rankings
from .inputs import Sources, code_version, digest, settings

JST = ZoneInfo("Asia/Tokyo")


def root(project):
    return project / "data/research/swing-screen"


def write(path, obj):
    atomic_write(
        path,
        json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2).encode(),
    )


def load(path):
    return json.loads(path.read_bytes())


def calendar(src, first, last):
    return src.calendar(
        str(pd.Timestamp(first).date() - timedelta(days=130)),
        str(pd.Timestamp(last).date() + timedelta(days=45)),
    )


def event_history(src, first, last):
    lo = str(pd.Timestamp(first).date() - timedelta(days=400))
    filings = src.table("financial", lo, last)
    schedule = src.table("schedule", lo, last)
    return filings, schedule


def events(f, day, financial, schedule):
    published = financial.loc[financial.DiscDate < day].sort_values(["DiscDate", "DiscNo"])
    releases = published.loc[published.DocType.str.contains("FinancialStatements", na=False)]
    forecasts = published.loc[
        published.DocType.str.contains("EarnForecastRevision", case=False, na=False)
    ]
    f["last_earnings"] = f.index.map(releases.groupby("Code").DiscDate.last())
    revisions = forecasts.groupby("Code").DiscDate.last()
    f["forecast_revision_date"] = f.index.map(revisions)
    f["forecast_revision_since_report"] = (
        f.forecast_revision_date.fillna("") >= f.last_earnings.fillna("~")
    ).where(f.last_earnings.notna())
    sched = schedule.loc[schedule.PubDate < day]
    if len(sched):
        newest = sched.groupby("Code").PubDate.transform("max")
        known = sched.loc[sched.PubDate.eq(newest) & sched.SchDate.gt(day)]
        by = known.groupby("Code").SchDate.agg(["min", "max"])
        f["next_earnings"] = f.index.map(by["min"].where(by["min"].eq(by["max"])))
    else:
        f["next_earnings"] = None
    return f


def _dates(src, cfg, through):
    cal = calendar(src, cfg["study_start"], through)
    return cal, [d for d in cal if cfg["study_start"] <= d <= through]


def build(project, folder, through, cfg):
    src = Sources(project, datetime.now(UTC))
    cal, days = _dates(src, cfg, through)
    if not days or days[-1] != through:
        raise ValueError("end date is not a saved session")
    folder.mkdir(parents=True, exist_ok=False)
    write(
        folder / "plan.json",
        dict(config=cfg, through=through, source_mode="retrospective", code=code_version()),
    )
    parts = []
    for year in sorted({d[:4] for d in days}):
        for half in (0, 1):
            dates = [d for d in days if d.startswith(year) and (int(d[5:7]) > 6) == bool(half)]
            if not dates:
                continue
            i = cal.index(dates[0])
            j = cal.index(dates[-1])
            # Use the available warmup from the archive start only.
            window = [d for d in cal[max(0, i - 65) : j + 1] if d >= cfg["earliest_archive"]]
            raw, index = src.blocks(window)
            feat = Features(raw, index, window)
            clip = raw.loc[raw.Date.isin(dates)].copy()
            keyed = clip.set_index(["Date", "Code"])
            for name in ("ma20", "atr", "m5", "m20", "eff20", "volume_ratio"):
                keyed[name] = (
                    feat.values[name].loc[dates].stack(future_stack=True).reindex(keyed.index)
                )
            part = year + ("-H2" if half else "-H1")
            keyed.reset_index().to_parquet(folder / (part + ".parquet"), index=False)
            ix = index.loc[dates].copy()
            ix["regime"] = feat.regime.loc[dates]
            decisions = {}
            for day in dates:
                decisions[day] = rankings(feat.day(day), cfg, day)
            write(folder / (part + ".json"), decisions)
            ix.to_parquet(folder / (part + "-topix.parquet"))
            parts.append(part)
            print(part, len(dates), flush=True)
            del raw, feat, keyed, clip
    financial, schedule = event_history(src, days[0], days[-1])
    financial.to_parquet(folder / "financial.parquet", index=False)
    schedule.to_parquet(folder / "schedule.parquet", index=False)
    write(
        folder / "manifest.json",
        dict(
            parts=parts,
            calendar=cal,
            days=days,
            inputs=src.refs,
            code=code_version(),
            hashes={f.name: digest(f) for f in folder.iterdir() if f.is_file()},
        ),
    )
    src.daily.cache_clear()


def daily(project, day, *, revision=False, preview=False, now=None, cfg=None):
    now = now or datetime.now(UTC)
    local = now.astimezone(JST)
    cfg = cfg or settings(project)
    home = root(project)
    if not preview and (home / "STOP").exists():
        return {"state": "stopped", "date": day}
    parent = home / ("previews" if preview else "daily") / day
    if not preview and (parent / "first.json").is_file() and not revision:
        return load(parent / "first.json")
    src = Sources(project, now)
    cal = calendar(src, day, day)
    if day not in cal:
        return {"state": "holiday", "date": day}
    j = cal.index(day)
    target = cal[j + 1]
    if not preview and (
        day > str(local.date())
        or (day == str(local.date()) and local.time() < time(16, 30))
        or (not revision or not (parent / "first.json").is_file())
        and local >= datetime.combine(pd.Timestamp(target).date(), time(9), JST)
    ):
        return {"state": "outside_publication_window", "date": day}
    if day not in src.receipts:
        return {"state": "awaiting_daily_receipt", "date": day}
    dates = cal[j - 65 : j + 1]
    raw, ix = src.blocks(dates)
    features = Features(raw, ix, dates)
    f = features.day(day)
    picks = rankings(f, cfg, day)
    fin, sched = event_history(src, day, day)
    f = events(f, day, fin, sched)
    public, plans = swing_earnings.asof(
        project / "data/swing-forward",
        min(now, datetime.combine(pd.Timestamp(day).date(), time.max, JST)),
    )
    if public["state"] == "verified":
        for c, rows in plans.items():
            dates_set = {r[0] for r in rows}
            if c in f.index and len(dates_set) == 1 and (v := next(iter(dates_set))) and v > day:
                f.loc[c, "next_earnings"] = v
    full = annotate(f, picks, cfg)
    with single_writer(home):
        if (parent / "first.json").is_file() and not revision:
            return load(parent / "first.json")
        parent.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(dir=parent))
        try:
            initial = not (parent / "first.json").is_file()
            full.to_parquet(temp / "features.parquet")
            # Key the recorded evidence to the snapshot, code and configuration.
            evidence = dict(inputs=src.refs, earnings=public, program=code_version(), config=cfg)
            write(temp / "inputs.json", evidence)
            key = digest(temp / "inputs.json")
            dest = parent / key
            feature_hash = digest(temp / "features.parquet")
            stamp = datetime.now(UTC)
            deadline = datetime.combine(pd.Timestamp(target).date(), time(9), JST)
            if not preview and initial and stamp >= deadline:
                return {"state": "publication_deadline_passed", "date": day}
            meta = dict(
                state="ready",
                date=day,
                target=target,
                asof=now.isoformat(),
                published=stamp.isoformat(),
                mode="research_preview" if preview else "forward",
                version=key,
                candidates=picks,
                regime=features.regime.at[day],
                universe=len(f),
                features_sha256=feature_hash,
            )
            if not dest.exists():
                write(temp / "summary.json", meta)
                os.rename(temp, dest)
            else:
                existing = load(dest / "summary.json")
                if (
                    existing["version"] != key
                    or existing["candidates"] != picks
                    or digest(dest / "features.parquet") != existing["features_sha256"]
                    or digest(dest / "inputs.json") != key
                ):
                    raise ValueError("existing publication differs from verified inputs")
                meta = existing
            if initial:
                if not preview and datetime.now(UTC) >= deadline:
                    return {"state": "publication_deadline_passed", "date": day}
                write(parent / "first.json", meta)
            return meta
        finally:
            if temp.exists():
                shutil.rmtree(temp)
            src.daily.cache_clear()
