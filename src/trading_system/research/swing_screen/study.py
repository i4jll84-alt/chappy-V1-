"""Sequential portfolio replay and independent open-to-open selection labels."""

import sqlite3
from collections import Counter, defaultdict, deque
from contextlib import closing
from datetime import timedelta

import numpy as np
import pandas as pd

from .. import intraday_replay
from .events import EventClock
from .inputs import code_version, digest
from .portfolio import Book, good, session
from .prepare import load, write

CORE = ("A", "B", "C", "D")


def specs(cfg):
    cases = {}
    for name in (*CORE, "matched", *(k + "_common" for k in CORE)):
        for cap in cfg["capital_levels"]:
            for hold in cfg["holds"]:
                cases[f"{name}:{cap}:{hold}"] = dict(method=name, capital=cap, hold=hold)
    for name in CORE:
        for suffix, extra in (
            ("limit", {"entry": "limit"}),
            ("continuation", {"entry": "continuation"}),
            ("trend", {"exit": "trend"}),
            ("atr", {"exit": "stop"}),
            ("trail", {"exit": "trail"}),
            ("risk", {"risk": True}),
            ("stress", {"stress": cfg["stress_extra"]}),
            ("sector_free", {"sector_slots": 0}),
        ):
            cases[name + ":" + suffix] = dict(
                method=name, capital=cfg["primary_capital"], hold=cfg["primary_hold"], **extra
            )
    for name in [
        *(m + s for m in CORE for s in ("_before_earnings", "_after_earnings")),
        "B60",
        "skip5",
        "high20",
        "high60",
        "efficiency",
        "industry",
        *(m + "_" + str(v) for m in "BCD" for v in cfg["liquidity_tests"] if v != cfg["liquidity"]),
    ]:
        cases[name] = dict(method=name, capital=cfg["primary_capital"], hold=cfg["primary_hold"])
    return cases


def intraday(project, day, codes):
    path = project / "data/research/shared-market" / (day + ".sqlite")
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        sig = intraday_replay.read_signals(
            db, [c[:4] for c in codes], day, intraday_replay.SESSIONS[0]
        )
        eligible = {
            r["symbol"]
            for r in sig
            if r["opening"] and r["recent_close"] and r["recent_close"] > r["opening"]
        }
        start = intraday_replay.stamp(day, "10:00")
        lo = intraday_replay.fmt(start)
        hi = intraday_replay.fmt(start + timedelta(minutes=5))
        observed = {}
        for q in intraday_replay.quotes(db, day):
            if (
                q["symbol"] in eligible
                and q["read_started_at"] >= lo
                and q["ask_at"] > lo
                and q["known_at"] <= hi
                and q["ask_qty"] >= 100
            ):
                observed.setdefault(q["jquants_code"], (q["ask"], q["ask_qty"]))
    return observed


def price_map(frame):
    return {r.Code: r for r in frame.itertuples(index=False)}


class Cohort:
    """Independent open-to-open labels for a single unlevered lot."""

    def __init__(self, day, index, rank, entry, benchmark, previous, regime, cfg):
        self.day, self.index, self.rank, self.benchmark, self.regime = (
            day,
            index,
            rank,
            benchmark,
            regime,
        )
        self.entries = {}
        self.invalid = {}
        for c in set().union(
            *(set(rank.get(k, [])) for k in (*CORE, "matched", *(m + "_common" for m in CORE)))
        ):
            b = entry.get(c)
            ref = previous.get(c)
            if b is None or not good(b.O) or not ref:
                self.invalid[c] = "missing_open"
            elif b.AdjFactor != 1 or b.ExRT not in ("", None, "0"):
                self.invalid[c] = "entry_right"
            elif b.UL == 1 and b.O == b.L == b.H:
                self.invalid[c] = "locked_upper"
            else:
                self.entries[c] = [b.O, 100.0, ref.S33, ref.m5]
        self.cfg = cfg

    def step(self, k, prices, bench):
        age = k - self.index
        if age <= 0:
            return []
        for c, entry in self.entries.items():
            if c in self.invalid:
                continue
            b = prices.get(c)
            if b is None or not good(b.AdjFactor):
                self.invalid[c] = "missing_or_delisted"
                continue
            if (b.ExRT or "") not in ("", "0", "1", "2"):
                self.invalid[c] = "unresolved_right"
                continue
            if b.AdjFactor != 1:
                shares = entry[1] / b.AdjFactor
                if (
                    b.ExRT not in ("1", "2")
                    or abs(shares - round(shares)) > 0.01
                    or round(shares) % 100
                ):
                    self.invalid[c] = "unresolved_right"
                else:
                    entry[1] = round(shares)
        if age not in self.cfg["holds"]:
            return []
        records = []
        for method in (*CORE, "matched", *(m + "_common" for m in CORE)):
            codes = self.rank.get(method, [])
            values = []
            net = []
            hot = []
            normal = []
            fails = Counter()
            for c in codes:
                b = prices.get(c)
                if c in self.invalid or not b or not good(b.O) or (b.LL == 1 and b.H == b.L == b.O):
                    fails[self.invalid.get(c, "exit_unfilled")] += 1
                    continue
                px, qty, _, m5 = self.entries[c]
                initial = 100 * px
                end = b.O * qty
                gross = end / initial - 1
                r = (end - initial - 2 * self.cfg["fee"] - (initial + end) * self.cfg["rate"]) / (
                    initial * (1 + self.cfg["rate"]) + self.cfg["fee"]
                )
                values.append(gross)
                net.append(r)
                (hot if m5 >= 0.10 else normal).append(r)
            avg = float(np.mean(net)) if net else None
            records.append(
                dict(
                    entry=self.day,
                    method=method,
                    hold=age,
                    regime=self.regime,
                    planned=len(codes),
                    observed=len(net),
                    missing=sum(fails.values()),
                    gross=np.mean(values) if values else None,
                    net=avg,
                    excess=avg - (bench / self.benchmark - 1) if avg is not None else None,
                    hot=np.mean(hot) if hot else None,
                    normal=np.mean(normal) if normal else None,
                    wins=sum(x > 0 for x in net),
                    gain=sum(x for x in net if x > 0),
                    loss=sum(x for x in net if x < 0),
                    nloss=sum(x < 0 for x in net),
                    failure=str(dict(fails)),
                )
            )
        return records


def replay_inputs(project, history, days):
    paths = {}
    base = project / "data/research/shared-market"
    for day in days:
        for ext in (".sqlite", ".sqlite-wal"):
            path = base / (day + ext)
            if path.is_file() and path.stat().st_size:
                paths[path.name] = digest(path)
    return dict(
        program=code_version(),
        received_intraday_files=paths,
        history=digest(history / "manifest.json"),
        received_signal_reader=digest(project / "src/trading_system/research/intraday_replay.py"),
    )


def run(project, history, out, cfg):
    manifest = load(history / "manifest.json")
    for file, h in manifest["hashes"].items():
        if digest(history / file) != h:
            raise ValueError("history artifact changed: " + file)
    out.mkdir(parents=True, exist_ok=False)
    evidence = replay_inputs(project, history, manifest["days"])
    cases = specs(cfg)
    events = EventClock(history)
    write(out / "cases.json", cases)
    books = {k: Book.start(s["capital"]) for k, s in cases.items()}
    before = {}
    prior_ranks = {}
    prior_regime = "unknown"
    cohorts = deque()
    days = manifest["days"]
    cal = manifest["calendar"]
    offsets = {d: i for i, d in enumerate(cal)}
    skipped = defaultdict(Counter)
    sizes = defaultdict(Counter)
    for part in manifest["parts"]:
        prices = pd.read_parquet(history / (part + ".parquet"))
        ranks = load(history / (part + ".json"))
        index = pd.read_parquet(history / (part + "-topix.parquet"))
        nav = []
        trades = []
        labels = []
        for day, group in prices.groupby("Date", sort=True):
            i = offsets[day]
            market = price_map(group)
            confirmed = intraday(
                project, day, {c for key in CORE for c in prior_ranks.get(key, [])}
            )
            if before:
                cohorts.append(
                    Cohort(
                        day, i, prior_ranks, market, index.at[day, "O"], before, prior_regime, cfg
                    )
                )
            for cohort in cohorts:
                labels.extend(cohort.step(i, market, index.at[day, "O"]))
            if cohorts and i - cohorts[0].index >= max(cfg["holds"]):
                cohorts.popleft()
            for case, s in cases.items():
                row, fs, skips = session(
                    books[case],
                    i,
                    day,
                    before,
                    market,
                    prior_ranks.get(s["method"], []),
                    cfg,
                    s,
                    confirmed,
                )
                row.update(case=case, regime=prior_regime, topix=index.at[day, "C"])
                nav.append(row)
                for f in fs:
                    trades.append(dict(date=day, case=case, **f))
                skipped[case].update(skips)
                sizes[case].update(selected=len(prior_ranks.get(s["method"], [])))
            events.screen(day, i, cal, market, before, ranks[day])
            before = market
            prior_ranks = ranks[day]
            prior_regime = index.at[day, "regime"]
        for fname, rows in [("nav", nav), ("trades", trades), ("labels", labels)]:
            pd.DataFrame(rows).to_parquet(out / (part + "-" + fname + ".parquet"), index=False)
        print("replayed", part, flush=True)
    pd.DataFrame(events.labels).to_parquet(out / "earnings-events.parquet", index=False)
    if evidence != replay_inputs(project, history, manifest["days"]):
        raise ValueError("research code or recorded inputs changed during replay")
    write(out / "evidence.json", evidence)
    write(
        out / "checks.json",
        dict(
            first=days[0],
            last=days[-1],
            sessions=len(days),
            cases=len(cases),
            skips={k: dict(v) for k, v in skipped.items()},
            pending_horizons={str(h): min(h, len(cohorts)) for h in cfg["holds"]},
            data_digest=digest(history / "manifest.json"),
            config=cfg,
        ),
    )
