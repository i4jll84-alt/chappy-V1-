"""Frozen source versions for retrospective research and receipt-bounded daily selection."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from ..a0_5_inputs import market_pages
from ..daytrade_universe import dated_inputs

TABLES = {
    "calendar": ("/markets/calendar", "Date HolDiv"),
    "bars": ("/equities/bars/daily", "Date Code O H L C Vo Va AdjFactor ExRT UL LL"),
    "master": ("/equities/master", "Date Code CoName Mkt S33 ProdCat"),
    "index": ("/indices/bars/daily/topix", "Date O H L C"),
    "financial": ("/fins/summary", "DiscDate Code DiscNo DocType"),
    "schedule": ("/fins/earnings-date", "PubDate Code SchDate"),
}


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def code_version():
    h = hashlib.sha256()
    for f in sorted(Path(__file__).parent.glob("*.py")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def validate(cfg):
    import math

    for key in (
        "cap_yen",
        "slots",
        "display",
        "atr_stop",
        "atr_trail",
        "position_fraction",
        "risk_fraction",
    ):
        if not math.isfinite(cfg[key]) or cfg[key] <= 0:
            raise ValueError("invalid setting " + key)
    if not (
        cfg["position_fraction"] <= 1
        and cfg["risk_fraction"] <= 1
        and 0 <= cfg["limit_discount"] < 1
    ):
        raise ValueError("invalid risk budget")
    if (
        cfg["liquidity_window"] not in (20, 60)
        or min(cfg["fee"], cfg["rate"], cfg["stress_extra"], cfg["liquidity"], cfg["sector_slots"])
        < 0
    ):
        raise ValueError("invalid cost or liquidity")
    if (
        min(cfg["capital_levels"]) <= 0
        or min(cfg["holds"]) < 1
        or max(cfg["holds"]) > cfg["embargo"]
    ):
        raise ValueError("invalid capital or label horizon")
    if (
        cfg["primary_hold"] not in cfg["holds"]
        or cfg["primary_capital"] not in cfg["capital_levels"]
        or not 0 < cfg.get("sector_fraction", 1) <= 1
    ):
        raise ValueError("primary comparison or sector budget outside configured range")
    return cfg


def settings(project):
    return validate(json.loads((project / "config/swing-screen.json").read_bytes()))


class Sources:
    def __init__(self, project, cutoff):
        self.project = project
        self.root = project / "data/jquants"
        self.history = self.root / "history"
        self.cutoff = cutoff
        self.refs = {}
        self.checked = set()
        dbpath = (self.history / "history.sqlite").resolve()
        with closing(sqlite3.connect(dbpath.as_uri() + "?mode=ro", uri=True)) as db:
            self.parts = db.execute(
                "SELECT endpoint,min_date,max_date,object_key,raw_path,sha256,parquet_path,"
                "parquet_sha256,available_at FROM bulk_files WHERE state='complete' "
                "AND julianday(available_at)<=julianday(?) "
                "ORDER BY modified,available_at,object_key",
                (cutoff.isoformat(),),
            ).fetchall()
        self.receipts = {}
        for p in sorted((self.root / "daily-snapshots").glob("*/last_collection.json")):
            receipt = json.loads(p.read_bytes())
            if (
                receipt["state"] == "complete"
                and datetime.fromisoformat(receipt["completed_at"]) <= cutoff
            ):
                self.receipts[receipt["date"]] = receipt

    def bulk(self, name, first, last):
        endpoint, names = TABLES[name]
        fields = names.split()
        key = fields[0]
        selected = {
            r[3]: r for r in self.parts if r[0] == endpoint and r[1] <= last and first <= r[2]
        }
        chunks = []
        for row in sorted(selected.values(), key=lambda x: (x[8], x[3])):
            _, _, _, obj, raw, h1, parquet, h2, stamp = row
            path = (self.history / parquet).resolve()
            if (obj, h2) not in self.checked:
                for rel, h in [(raw, h1), (parquet, h2)]:
                    f = (self.history / rel).resolve()
                    if not f.is_relative_to(self.history.resolve()) or digest(f) != h:
                        raise ValueError("archive hash mismatch: " + obj)
                self.checked.add((obj, h2))
            self.refs[obj] = dict(raw=h1, parquet=h2, available_at=stamp)
            chunks.append(
                pq.read_table(
                    path, columns=fields, filters=[(key, ">=", first), (key, "<=", last)]
                ).to_pandas()
            )
        x = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame(columns=fields)
        return x.drop_duplicates().reset_index(drop=True)

    @lru_cache(maxsize=64)
    def daily(self, day):
        raw, master, refs = dated_inputs(self.root, [day], self.cutoff)
        rec = self.receipts[day]
        b = [v[0] for v in raw.values()]
        ix = market_pages(self.root, rec, "topix", "Date", self.cutoff, refs)
        fin = []
        if "financials" in rec["jobs"]:
            fin = market_pages(self.root, rec, "financials", "DiscDate", self.cutoff, refs)
        else:
            refs.append(dict(dataset="financials", date=day, state="unavailable"))
        self.refs["daily/" + day] = dict(completed_at=rec["completed_at"], sources=refs)
        return {"bars": b, "master": list(master.values()), "index": ix, "financial": fin}

    def table(self, name, first, last):
        x = self.bulk(name, first, last)
        field = TABLES[name][1].split()[0]
        days = [d for d in self.receipts if first <= d <= last]
        if name in ("bars", "master", "index", "financial") and days:
            extra = [r for d in days for r in self.daily(d)[name]]
            x = pd.concat(
                [x.loc[~x[field].isin(days)], pd.DataFrame(extra, columns=TABLES[name][1].split())],
                ignore_index=True,
            )
        keys = {
            "bars": ["Date", "Code"],
            "master": ["Date", "Code"],
            "calendar": ["Date"],
            "index": ["Date"],
            "financial": ["DiscDate", "Code", "DiscNo"],
            "schedule": ["PubDate", "Code", "SchDate"],
        }[name]
        if x.duplicated(keys).any():
            raise ValueError("conflicting source versions: " + name)
        return x

    def calendar(self, first, last):
        days = self.table("calendar", first, last).set_index("Date").HolDiv
        expected = pd.date_range(first, last).strftime("%Y-%m-%d")
        if not set(expected) <= set(days.index) or not days.isin(["0", "1", "2", "3"]).all():
            raise ValueError("incomplete exchange calendar")
        return sorted(days.index[days.isin(["1", "2"])])

    def blocks(self, dates):
        first, last = dates[0], dates[-1]
        bars = self.table("bars", first, last)
        master = self.table("master", first, last)
        index = self.table("index", first, last).set_index("Date").reindex(dates)
        if (
            not set(dates) <= set(master.Date)
            or not set(dates) <= set(bars.Date)
            or index.C.isna().any()
        ):
            raise ValueError("missing full market session " + first + ".." + last)
        daily = master.merge(bars, on=["Date", "Code"], how="outer", validate="one_to_one")
        for key in ("O", "H", "L", "C", "Vo", "Va", "AdjFactor", "UL", "LL"):
            daily[key] = pd.to_numeric(daily[key], errors="coerce")
        for key in ("O", "H", "L", "C"):
            index[key] = pd.to_numeric(index[key], errors="coerce")
        return daily, index
