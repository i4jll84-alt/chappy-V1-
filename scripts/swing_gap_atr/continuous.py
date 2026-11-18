"""Replay frozen swing ranks in continuous paper accounts. No writes to the source project.

Run with the V1 project Python, e.g. python -B continuous.py --project <V1> --output <results>.
The archive run attempts every archived decision until that specific ledger is unpriceable.
A preserved common-window snapshot is also produced as of 2023-12-27.
"""

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal as D
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch

import duckdb


def read(path):
    return json.loads(path.read_bytes())


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def file_hash(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def validate_hash(path, expected):
    if file_hash(path) != expected:
        raise ValueError(f"Input hash mismatch: {path.name}")


def write_csv(path, data):
    fields = list(dict.fromkeys(k for row in data for k in row))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(data)


def frozen_ranks(archive, study):
    archive_ranks = defaultdict(lambda: {"current": [], "plus3": []})
    proof = read(archive / "agent-seal.json")
    validate_hash(archive / "agent-output.json", proof["sha256"])
    decoded = read(archive / "decode.json")
    sealed_dates = {v["date"] for v in decoded["sets"].values()}
    for row in rows(study / "selection.csv"):
        if row["decision"] not in sealed_dates:
            continue
        key = {"old": "current", "new": "plus3"}[row["arm"]]
        ranked = archive_ranks[row["decision"]][key]
        assert int(row["rank"]) == len(ranked) + 1
        ranked.append(row["code"])
    for token, result in read(archive / "agent-output.json").items():
        meta = decoded["sets"][token]
        validate_hash(archive / "inputs" / (token + ".parquet"), meta["sha"])
        for arm in ("current", "plus3"):
            assert archive_ranks[meta["date"]][arm] == [decoded["ids"][k] for k in result[arm]]
    assert len(archive_ranks) == proof["sets"] == 730
    return dict(sorted(archive_ranks.items()))


def november_ranks(sealed):
    path = sealed / "agent-decisions.json"
    validate_hash(path, read(sealed / "summary.json")["raw_answer_sha"])
    ranks = {}
    credits = {}
    for tag, choices in read(path).items():
        audit = read(sealed / "audit" / (tag + ".json"))
        validate_hash(sealed / "inputs" / (tag + ".csv"), audit["input_sha"])
        decision = audit["decision"]
        credits[decision] = audit["credit_complete"]
        ranks[decision] = {}
        for arm in ("current", "plus3"):
            selected = [
                r["code"]
                for r in audit["machine_rows"]
                if arm == "current" or not r["plus3_exclusions"]
            ][:5]
            assert selected == [audit["ids"][k] for k in choices[arm]]
            ranks[decision][arm] = selected
    return dict(sorted(ranks.items())), credits


def november_extension(project, sf, ranks, credits):
    study = project / "data/offline-verification/daily-swing-through-20261113"
    day = "2026-11-12"
    evidence = next(x for x in read(study / "source-checks.json") if x["date"] == day)
    cutoff = datetime.fromisoformat(day + "T23:59:59+09:00")
    signal = sf.screen(project, cutoff.date(), cutoff)
    assert signal["date"] == day and signal["input_refs"] == evidence["prices"]
    assert not signal["credit_complete"] and all("issue" in x for x in evidence["credit"])
    ranks[day] = {"current": [r["code"] for r in signal["candidates"]][:5], "plus3": []}
    credits[day] = False
    asof = {"2026-11-13": "2026-11-13T17:20:50+09:00"}
    val_sources = {"2026-11-13": evidence["valuation_sources"]}
    proofs = {p.name: file_hash(p) for p in [study / "source-checks.json", study / "daily.csv"]}
    for entry in ("2026-11-16", "2026-11-17", "2026-11-18"):
        folder = project / "data/swing-forward/sessions" / entry
        pre, review, close = [
            read(folder / file) for file in ("preopen.json", "review-input.json", "close.json")
        ]
        assert sf.signature(pre) == review["input_sha256"]
        assert pre["target"] == close["target"] == entry and not pre["stopped"]
        assert datetime.fromisoformat(pre["recorded_at"]) < datetime.fromisoformat(
            entry + "T09:00:00+09:00"
        )
        signal = pre["signal"]
        day = signal["date"]
        assert day not in ranks and pre["target"] == entry
        credits[day] = signal["credit_complete"]
        ranks[day] = {
            arm: [
                r["code"]
                for r in signal["candidates"]
                if arm == "current" or (credits[day] and not r["plus3_exclusions"])
            ][:5]
            for arm in ("current", "plus3")
        }
        asof[entry] = close["recorded_at"]
        val_sources[entry] = close["sources"]
        for p in [folder / file for file in ("preopen.json", "review-input.json", "close.json")]:
            proofs[str(p.relative_to(project))] = file_hash(p)
    return asof, val_sources, proofs


class Market:
    def __init__(self, project, study, codes, cutoffs, expected_sources):
        self.cutoffs = cutoffs
        self.expected_sources = expected_sources
        self.project = project
        self.root = project / "data/jquants/history"
        self.codes = set(codes)
        self.evidence = read(study / "sources.json")
        self.files = {}
        wal = self.root / "history.sqlite-wal"
        if wal.exists() and wal.stat().st_size:
            raise ValueError("History database has uncheckpointed data")
        with sqlite3.connect(
            (self.root / "history.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True
        ) as con:
            files = con.execute(
                "SELECT endpoint,min_date,max_date,parquet_path,parquet_sha256 "
                "FROM bulk_files WHERE state='complete' AND endpoint IN (?,?) "
                "AND max_date >= '2023-10-01' AND min_date <= '2026-10-30'",
                ("/equities/bars/daily", "/markets/calendar"),
            ).fetchall()
        self.db = duckdb.connect()
        self.db.execute("SET threads=2")
        self.db.execute("CREATE TEMP TABLE needed (Code VARCHAR PRIMARY KEY)")
        self.db.executemany("INSERT INTO needed VALUES (?)", [(c,) for c in sorted(self.codes)])
        self.used_hashes = {}
        calendars = []
        for endpoint, first, last, path, sha in files:
            if endpoint == "/markets/calendar":
                calendars.append(self.checked(path, sha))
            else:
                self.files.setdefault(first[:7], []).append((path, sha))
        self.db.from_parquet(calendars).create_view("calendar")
        self.sessions = [
            r[0]
            for r in self.db.execute(
                "SELECT DISTINCT Date FROM calendar WHERE HolDiv IN ('1','2') ORDER BY Date"
            ).fetchall()
        ]
        self.indices = {d: i for i, d in enumerate(self.sessions)}

    def checked(self, rel, sha):
        p = self.root / rel
        original = str(p.relative_to(self.project)).replace("/", chr(92))
        assert self.evidence[original] == sha
        if rel not in self.used_hashes:
            validate_hash(p, sha)
            self.used_hashes[rel] = sha
        return str(p)

    @lru_cache(maxsize=3)
    def monthly(self, month):
        self.db.from_parquet([self.checked(p, h) for p, h in self.files[month]]).create_view(
            "quotes", replace=True
        )
        out = defaultdict(dict)
        for date, code, op, hi, lo, cl, f, x in self.db.execute(
            "SELECT b.Date,b.Code,b.O,b.H,b.L,b.C,b.AdjFactor,b.ExRT "
            "FROM quotes b JOIN needed USING(Code)"
        ).fetchall():
            assert code not in out[date]
            out[date][code] = dict(O=op, H=hi, L=lo, C=cl, AdjFactor=f, ExRT=x)
        return out

    @lru_cache(maxsize=32)
    def bars(self, day):
        if day <= "2026-10-30":
            return self.monthly(day[:7]).get(day, {})
        from trading_system.research.daytrade_universe import dated_inputs

        data, _, sources = dated_inputs(
            self.project / "data/jquants",
            [day],
            datetime.fromisoformat(self.cutoffs.get(day, "2026-11-13T15:37:10+09:00")),
        )
        if day in self.expected_sources:
            assert sources == self.expected_sources[day]
        for s in sources:
            if "manifest" in s:
                self.used_hashes[s["manifest"]] = s["sha256"]
        return {k: v[0] for k, v in data.items() if k in self.codes}

    @lru_cache(maxsize=8000)
    def atr(self, signal, code):
        i = self.indices[signal]
        window = [self.bars(day).get(code) for day in self.sessions[i - 14 : i + 1]]
        if len(window) != 15 or any(bar is None for bar in window):
            return None
        previous = positive(window[0].get("C"))
        total = D(0)
        for bar in window[1:]:
            high, low, close = (positive(bar.get(k)) for k in ("H", "L", "C"))
            if (
                any(x is None for x in (previous, high, low, close))
                or high < low
                or not regular(bar)
            ):
                return None
            total += max(high - low, abs(high - previous), abs(low - previous))
            previous = close
        return total / 14 if total > 0 else None


def positive(value):
    if value in ("", None):
        return None
    dec = D(str(value))
    return dec if dec.is_finite() and dec > 0 else None


def regular(bar):
    return positive(bar.get("AdjFactor")) == 1 and bar.get("ExRT") in ("", None)


class IncompleteDay(Exception):
    pass


def proposed(sf, state, orders, bars, entry, due, limits, atr):
    """Keep V1 opening planning. Overnight stop sells can fund only existing plans.
    Intraday stop proceeds become available after all opening buys and stay until next plan.
    """
    from copy import deepcopy

    state = deepcopy(state)
    positions = state["positions"]
    for c in positions:
        if not (bar := bars.get(c)) or not regular(bar):
            raise IncompleteDay(f"{entry}: {c} holding right or missing row")
    events = []
    cash, realized, fees, slip = (D(state[k]) for k in ("cash", "realized", "fees", "slippage"))

    def sell(code, px, why):
        nonlocal cash, realized, fees, slip
        value = px * sf.LOT
        credit = value * (1 - sf.SLIP) - sf.FEE
        gain = credit - D(positions.pop(code)["debit"])
        cash += credit
        realized += gain
        fees += sf.FEE
        slip += value * sf.SLIP
        events.append(
            dict(action="sell", code=code, status="simulated", price=px, reason=why, pnl=gain)
        )

    for code in list(positions):
        bar = bars[code]
        op = positive(bar.get("O"))
        if op is None:
            if any(positive(bar.get(k)) is not None for k in ("H", "L", "C")):
                raise IncompleteDay(f"{entry}: {code} missing opening with price activity")
            raise IncompleteDay(f"{entry}: {code} missing held-session quote")
        if code in orders["sell"]:
            sell(code, op, "time")
        elif op <= D(positions[code]["stop"]):
            sell(code, op, "gap_stop")
    for code in orders["buy"]:
        bar = bars.get(code, {})
        op = positive(bar.get("O"))
        if op is None:
            events.append(dict(code=code, action="buy", status="missing_open"))
        elif op > limits[code]:
            events.append(
                dict(
                    code=code, action="buy", status="above_open_limit", price=op, limit=limits[code]
                )
            )
        elif sf.cost(op) > cash:
            events.append(dict(code=code, action="buy", status="cash_short"))
        elif not regular(bar):
            raise IncompleteDay(f"{entry}: {code} opening right on proposed purchase")
        else:
            debit = sf.cost(op)
            stop = op - 2 * atr[code]
            positions[code] = dict(
                entry=entry, exit=due, debit=str(debit), quantity=sf.LOT, stop=str(stop)
            )
            cash -= debit
            fees += sf.FEE
            slip += op * sf.LOT * sf.SLIP
            events.append(
                dict(
                    code=code,
                    action="buy",
                    status="simulated",
                    price=op,
                    stop=stop,
                    atr=atr[code],
                    limit=limits[code],
                )
            )
    for code in list(positions):
        low = positive(bars[code].get("L"))
        if low is None:
            raise IncompleteDay(f"{entry}: {code} missing held-session low")
        if low <= D(positions[code]["stop"]):
            sell(code, D(positions[code]["stop"]), "intraday_stop")
    close = {c: positive(bars[c].get("C")) for c in positions}
    if any(v is None for v in close.values()):
        raise IncompleteDay(f"{entry}: missing close on held position")
    nav = cash + sum(cl * sf.LOT for cl in close.values())
    high = max(D(state["high"]), nav)
    dd = max(D(state["max_dd"]), 1 - nav / high)
    unreal = sum(close[c] * sf.LOT - D(pos["debit"]) for c, pos in positions.items())
    assert abs(realized + unreal - (nav - sf.CAPITAL)) < D("1e-15")
    state.update(
        cash=str(cash),
        realized=str(realized),
        fees=str(fees),
        slippage=str(slip),
        high=str(high),
        max_dd=str(dd),
    )
    return (
        state,
        events,
        dict(
            nav=nav,
            net=nav - sf.CAPITAL,
            realized=realized,
            unrealized=unreal,
            fees=fees,
            slippage=slip,
            max_dd_pct=dd * 100,
            return_pct=(nav / sf.CAPITAL - 1) * 100,
        ),
    )


def simulate(sf, market, ranks, arm, period, control, cutoff=None):
    base = "current" if arm == "current" else "plus3"
    managed = arm == "plus3_limit_atr"
    state = sf.initial()["machine_" + base]
    daily = []
    records = []
    issue = None
    count = Counter()
    closed_results = []
    control_checked = 0
    prior_entry = None
    for d, arms in ranks.items():
        i = market.indices[d]
        entry, due = market.sessions[i + 1], market.sessions[i + 7]
        if cutoff and entry > cutoff:
            break
        if prior_entry is not None:
            assert d == prior_entry, (d, prior_entry)
        prior_entry = entry
        signal_bars = market.bars(d)
        bars = market.bars(entry)
        rows_for_plan = []
        limits, atr = {}, {}
        events = []
        for code in arms[base]:
            close = positive(signal_bars[code]["C"])
            assert close is not None
            if managed:
                measure = market.atr(d, code)
                if measure is None and code not in state["positions"]:
                    events.append(dict(code=code, action="buy", status="atr_unavailable"))
                    continue
                atr[code] = measure
                limits[code] = close * D("1.03")
            rows_for_plan.append(dict(code=code, estimated_debit=str(sf.cost(close))))
        orders = sf.account_orders(state, rows_for_plan, entry)
        events.extend(dict(code=c, action="buy", status=why) for c, why in orders["skips"].items())
        prev_positions = state["positions"]
        try:
            if managed:
                next_state, fills, metrics = proposed(
                    sf, state, orders, bars, entry, due, limits, atr
                )
            else:
                next_state, fills, metrics = sf.settle_account(state, orders, bars, entry, due)
                if metrics is None:
                    raise IncompleteDay(next_state.get("reason", f"{entry}: missing held close"))
                for fill in fills:
                    if fill["action"] == "sell" and fill["status"] == "simulated":
                        gross = D(fill["open"]) * sf.LOT
                        fill["pnl"] = (
                            gross
                            - D(fill["fee"])
                            - D(fill["slippage"])
                            - D(prev_positions[fill["code"]]["debit"])
                        )
                        fill["reason"] = "time"
            events.extend(fills)
        except IncompleteDay as err:
            issue = str(err)
            break
        state = next_state
        for event in events:
            record = dict(sample=period, arm=arm, decision=d, session=entry, **event)
            records.append(record)
            key = event["status"] if event["status"] != "simulated" else event["action"]
            count[key] += 1
            if "pnl" in event:
                closed_results.append(D(str(event["pnl"])))
                count[event["reason"]] += 1
        if not managed and (entry, base) in control:
            expected = control[entry, base]
            assert all(D(expected[k]) == D(str(v)) for k, v in metrics.items())
            control_checked += 1
        daily.append(
            dict(
                sample=period,
                arm=arm,
                decision=d,
                session=entry,
                **metrics,
                buys=count["buy"],
                sells=count["sell"],
                limit_skips=count["above_open_limit"],
                stops=count["gap_stop"] + count["intraday_stop"],
                atr_skips=count["atr_unavailable"],
                cash=state["cash"],
                positions=len(state["positions"]),
                winning_sells=sum(x > 0 for x in closed_results),
                losing_sells=sum(x < 0 for x in closed_results),
                gross_profit=sum(x for x in closed_results if x > 0),
                gross_loss=-sum(x for x in closed_results if x < 0),
            )
        )
    return (
        daily,
        records,
        dict(
            last=daily[-1] if daily else None,
            stop=issue,
            count=dict(count),
            baseline_days_checked=control_checked,
        ),
    )


def reference(path, capital):
    return {
        (r["entry"], r["account"][8:]): r
        for r in rows(path)
        if r["account"].startswith("machine_") and int(r["capital"]) == capital
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project = args.project.resolve()
    sys.path.insert(0, str(project / "src"))
    from trading_system.research import swing_forward as sf

    parent = project / "data/offline-verification"
    study = parent / "swing-periods-ma-jsf-20261113/outputs"
    sealed = parent / "swing-agent-replay-20261113"
    previous = parent / "swing-capital-20261116"
    archives = frozen_ranks(sealed / "archive", study)
    nov, credit = november_ranks(sealed)
    cutoffs, expected_sources, rank_evidence = november_extension(project, sf, nov, credit)
    old_nov = reference(previous / "november-nav.csv", 2000000)
    latest_reference = rows(parent / "daily-swing-through-20261113/daily.csv")
    for row in latest_reference:
        key = (row["day"], row["rule"])
        if key in old_nov:
            assert all(
                D(row[k]) == D(old_nov[key][k])
                for k in ("net", "nav", "realized", "unrealized", "fees", "slippage", "max_dd_pct")
            )
        old_nov[key] = row
    codes = {
        c
        for sample in (archives, nov)
        for arms in sample.values()
        for rank in arms.values()
        for c in rank
    }
    market = Market(project, study, codes, cutoffs, expected_sources)
    args.output.mkdir(parents=True, exist_ok=True)
    daily, trades, summary = [], [], {}
    for period, ranks, control in [
        ("archive", archives, {}),
        ("november", nov, old_nov),
    ]:
        for arm in ("current", "plus3", "plus3_limit_atr"):
            nav, records, study_result = simulate(sf, market, ranks, arm, period, control)
            daily.extend(nav)
            trades.extend(records)
            summary[period + "/" + arm] = study_result
            if period == "november":
                summary["november_1112/" + arm] = next(
                    x for x in nav if x["session"] == "2026-11-12"
                )
                assert len(nav) == 11 and nav[-1]["session"] == "2026-11-18"
            if period == "archive":
                summary["common/" + arm] = next(x for x in nav if x["session"] == "2023-12-27")
    for cap in (1_000_000, 3_000_000):
        control = reference(previous / "archive-nav.csv", cap)
        with patch.object(sf, "CAPITAL", D(cap)):
            for arm in ("current", "plus3"):
                nav, _, details = simulate(
                    sf, market, archives, arm, "control", control, cutoff="2023-12-27"
                )
                assert len(nav) == details["baseline_days_checked"] == 38
    assert all(summary["november/" + a]["baseline_days_checked"] == 8 for a in ("current", "plus3"))
    summary["inputs"] = dict(
        archive_decisions=len(archives),
        november_decisions=len(nov),
        november_credit=credit,
        control="152 archived daily baseline valuations at 1m/3m, 16 November valuations at 2m",
        quote_hashes=market.used_hashes,
        rank_evidence=rank_evidence,
    )
    write_csv(args.output / "nav.csv", daily)
    write_csv(args.output / "events.csv", trades)
    (args.output / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print("Archive: common daily metrics verified. November: all daily baseline controls verified.")
    for k, v in summary.items():
        if k.startswith("common/") or k.startswith("november/"):
            row = v.get("last", v)
            print(k, row["session"], row["net"], row["max_dd_pct"], row["buys"], row["sells"])
        elif k.startswith("archive/"):
            print(k, "first unresolved:", v["stop"])


if __name__ == "__main__":
    main()
