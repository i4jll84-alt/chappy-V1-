"""Replay ranked picks independently from raw quotes with the supplied continuous-account code."""

import argparse
import csv
import hashlib
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from fractions import Fraction
from importlib.machinery import SourceFileLoader
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

D = Decimal
FIRST, RECENT, LAST = "2016-11-18", "2023-10-31", "2026-11-18"
FIELDS = ("O", "H", "L", "C", "AdjFactor", "ExRT")
ATTACHMENT_SHA = "a74c366389a0c6502fd09b28a19ae1d5761d7b0f7f993b72cc97c806369a8533"
CAPITALS = (1_000_000, 2_000_000, 3_000_000)


def read_json(path):
    return json.loads(path.read_bytes())


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_json(path, obj):
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def write_csv(path, data):
    fields = list(dict.fromkeys(k for r in data for k in r))
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        w = csv.DictWriter(file, fieldnames=fields)
        w.writeheader()
        w.writerows(data)


def archived_selections(base):
    old = base / "swing-position-caps-20261118"
    recent = base / "swing-decade-capitals-20261118"
    j = read_json(recent / "boundary/boundary_proof.json")
    assert j["complete"] and j["decision"] == RECENT
    picks = {RECENT: j["selected_codes"]}
    for row in read_csv(old / "candidates.csv"):
        if row["signal"] > "2026-10-30":
            continue
        lst = picks.setdefault(row["signal"], [])
        assert int(row["rank"]) == len(lst) + 1
        lst.append(row["code"])
    for row in read_csv(recent / "extended/ranks.csv"):
        picks[row["decision"]] = row["refreshed_codes"].split()
    return picks


def market_inputs(project, ranks, out):
    hist = project / "data/jquants/history"
    man = read_json(hist / "quality-20261031.json")["input_manifest"]
    files = defaultdict(list)
    for row in man:
        ep = row["endpoint"]
        if ep in ("/equities/bars/daily", "/markets/calendar"):
            path = hist / row["parquet_path"]
            assert digest(path) == row["parquet_sha256"]
            files[ep].append(str(path))
    hashes = {}
    with duckdb.connect() as db:
        db.execute("SET threads=2")
        db.from_parquet(files["/markets/calendar"]).create_view("business_calendar")
        sessions = [
            r[0]
            for r in db.execute(
                "SELECT Date FROM business_calendar WHERE HolDiv IN ('1','2') ORDER BY Date"
            ).fetchall()
        ]
        positions = {d: i for i, d in enumerate(sessions)}
        window = sessions[positions[FIRST] : positions[LAST] + 1]
        assert len(window) == 2441
        all_codes = defaultdict(set)
        for row in read_csv(ranks / "candidates.csv"):
            all_codes[row["decision"]].add(row["code"])
        for d, codes in archived_selections(project / "data/offline-verification").items():
            all_codes[d].update(codes)
        needed = set()
        for d, codes in all_codes.items():
            i = positions[d]
            needed.update((t, c) for c in codes for t in sessions[i - 14 : i + 8] if t <= LAST)
        days, codes = zip(*sorted(needed), strict=True)
        db.register("wanted", pa.table(dict(day=days, code=codes)))
        db.from_parquet(files["/equities/bars/daily"]).create_view("prices")
        tab = db.execute(
            "SELECT p.Date,p.Code,p.O,p.H,p.L,p.C,p.AdjFactor,p.ExRT FROM prices p "
            "JOIN wanted w ON p.Date=w.day AND p.Code=w.code ORDER BY p.Date,p.Code"
        ).fetch_arrow_table()
    more = []
    for day in window:
        if day <= "2026-10-30":
            continue
        root = project / "data/jquants/daily-snapshots" / day
        rec = read_json(root / "last_collection.json")
        meta = Path(rec["daily_manifest"])
        assert rec["state"] == "complete" and meta.resolve().is_relative_to(root.resolve())
        rows = read_json(meta)
        assert rows["state"] == "verified"
        hashes[day] = digest(meta)
        for pg in rows["pages"]:
            path = meta.parent / pg["file"]
            assert digest(path) == pg["sha256"]
            for q in read_json(path)["data"]:
                assert q["Date"] == day
                if (day, q["Code"]) in needed:
                    more.append(
                        {
                            k: (str(q[k]) if q.get(k) not in (None, "") else "")
                            for k in ("Date", "Code", *FIELDS)
                        }
                    )
    merged = pa.concat_tables([tab, pa.Table.from_pylist(more, schema=tab.schema)])
    pq.write_table(merged, out / "audit_quotes.parquet", compression="zstd")
    write_json(out / "audit_calendar.json", sessions)
    write_json(
        out / "quote_coverage.json",
        dict(
            price_parquets=len(files["/equities/bars/daily"]),
            daily_manifests=hashes,
            required_pairs=len(needed),
            quote_rows=len(merged),
        ),
    )


def required_sources(project, path):
    proof = project / "data/offline-verification/swing-position-caps-20261118/inputs.json"
    items = {(r["code"], r["date"]): D(r["ratio"]) for r in read_json(proof)["corporate_actions"]}
    if path:
        for row in read_json(path):
            assert row["notice_date"] < row["ex_date"]
            items[row["code"], row["ex_date"]] = D(str(row["quantity_multiplier"]))
    return items


def normalized_ratio(raw):
    f = D(str(raw))
    frac = Fraction(1 / f).limit_denominator(1000)
    rat = D(frac.numerator) / frac.denominator
    return rat if abs(f * rat - 1) <= D("0.00001") else 1 / f


def prep(project, ranks, out, verified):
    dates = read_json(out / "audit_calendar.json")
    ix = {d: i for i, d in enumerate(dates)}
    requested = read_csv(ranks / "signals.csv")
    assert [r["decision"] for r in requested] == dates[ix[FIRST] : ix[LAST]]
    nojsf = {r["decision"]: [] for r in requested}
    expected = {}
    for row in read_csv(ranks / "candidates.csv"):
        d, c = row["decision"], row["code"]
        assert int(row["rank"]) == len(nojsf[d]) + 1
        nojsf[d].append(c)
        expected[d, c] = row
    jsf = archived_selections(project / "data/offline-verification")
    data = pq.read_table(out / "audit_quotes.parquet").to_pylist()
    raw = {}
    values = defaultdict(list)
    for r in data:
        d, c = r["Date"], r["Code"]
        items = [D(v) if (v := r[k]) else None for k in FIELDS[:-1]]
        raw[d, c] = (*items, r["ExRT"])
        op, hi, lo, cl, f = items
        ratio = D(1)
        if f is not None and f != 1 and r["ExRT"] in ("1", "2"):
            ratio = verified.get((c, d), normalized_ratio(f))
        values[d].append(
            dict(
                code=c,
                Open=float(op) if op else None,
                Low=float(lo) if lo else None,
                Close=float(cl) if cl else None,
                AdjustmentFactor=1 / float(ratio),
            )
        )
    assert len(raw) == len(data)
    frames = {}
    for d in dates[ix[FIRST] : ix[LAST] + 1]:
        frames[d] = pd.DataFrame(
            values[d]
            or [
                dict(
                    code="_calendar_",
                    Open=None,
                    Low=None,
                    Close=None,
                    AdjustmentFactor=1.0,
                )
            ]
        ).set_index("code")
    actions = []
    rank_groups = []
    capfields = Counter()
    for group, names_by_date in (("nojsf", nojsf), ("jsf", jsf)):
        signals = {}
        for d, names in names_by_date.items():
            assert len(names) <= 5
            i = ix[d]
            cands = []
            for c in names:
                bars = [raw.get((t, c)) for t in dates[i - 14 : i + 1]]

                def valid(x):
                    return x is not None and x > 0

                atr = None
                if (
                    all(bars)
                    and valid(bars[0][3])
                    and all(
                        all(map(valid, (r[1], r[2], r[3])))
                        and r[1] >= r[2]
                        and r[4] == 1
                        and not r[5]
                        for r in bars[1:]
                    )
                ):
                    trs = [
                        max(b[1] - b[2], abs(b[1] - a[3]), abs(b[2] - a[3]))
                        for a, b in zip(bars, bars[1:])
                    ]
                    avg = sum(trs) / 14
                    atr = avg if avg > 0 else None
                reference = bars[-1][3] if bars[-1] else None
                if group == "nojsf":
                    e = expected[d, c]
                    if reference != D(e["prev_close"]):
                        raise ValueError(f"Candidate reference price mismatch {d}/{c}")
                    if atr is None:
                        assert not e["atr14"], (d, c, e)
                    else:
                        assert e["atr14"] and abs(D(e["atr14"]) - atr) < D("0.000001"), (
                            d,
                            c,
                            e["atr14"],
                            atr,
                        )
                    capfields["verified_candidate_atrs"] += 1
                if atr is None or reference is None:
                    continue
                first = dates[i + 1]
                entry = raw.get((first, c))
                if entry and valid(entry[0]) and (entry[4] != 1 or entry[5]):
                    ratio = verified.get((c, first))
                    actions.append(
                        dict(
                            day=first,
                            code=c,
                            group=group,
                            factor=entry[4],
                            flag=entry[5],
                            at_entry=True,
                        )
                    )
                    if not ratio:
                        continue
                    reference /= ratio
                    atr /= ratio
                cands.append(dict(code=c, prev_close=float(reference), atr14=float(atr)))
                for t in dates[i + 1 : i + 8]:
                    val = raw.get((t, c))
                    if val and (val[4] != 1 or val[5]):
                        actions.append(
                            dict(
                                day=t,
                                code=c,
                                group=group,
                                factor=val[4],
                                flag=val[5],
                                at_entry=(t == first),
                            )
                        )
            signals[d] = cands
        rank_groups.append(signals)
    write_csv(out / "possible_actions.csv", actions)
    write_json(
        out / "rank_input_checks.json",
        dict(
            **capfields,
            ohlc_pairs=len(raw),
            possible_actions=len({(r["day"], r["code"]) for r in actions}),
        ),
    )
    return dates, ix, frames, rank_groups


def load_logic(path):
    assert digest(path) == ATTACHMENT_SHA
    spec = importlib.util.spec_from_loader(
        "user_cap_strategy", SourceFileLoader("user_cap_strategy", str(path))
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def select_tradable_lots(signals, dates, ix, board_lots):
    allowed = defaultdict(list)
    for r in read_csv(board_lots):
        if r["unit"] and 100 % int(r["unit"]) == 0:
            allowed[r["code"]].append((r["start"], r["end"]))
    result = {}
    for d, picks in signals.items():
        entry = dates[ix[d] + 1]
        if entry >= "2018-10-01":
            result[d] = [p for p in picks if p["code"] != "18680"]
        else:
            result[d] = [
                p for p in picks if any(start <= entry <= end for start, end in allowed[p["code"]])
            ]
    return result


def replay(project, ranks, out, verified_json, attachment, board_lots):
    verified = required_sources(project, verified_json)
    dates, ix, frames, (nojsf, jsf) = prep(project, ranks, out, verified)
    user = load_logic(attachment)
    navs = []
    fills = []
    specs = (
        ("decade_nojsf", FIRST, nojsf),
        ("recent_nojsf", RECENT, nojsf),
        ("recent_jsf", RECENT, jsf),
    )
    prefix = ""
    if board_lots:
        specs = (
            ("decade_confirmed_lots", FIRST, select_tradable_lots(nojsf, dates, ix, board_lots)),
        )
        prefix = "lot_"
    for scope, first, signals in specs:
        sessions = dates[ix[first] : ix[LAST] + 1]
        for cap in CAPITALS:
            cfg = user.CapConfig(name=scope, max_positions=None, initial_capital=cap)
            result = user.simulate_continuous_account(sessions, frames, signals, cfg)
            marks = result["nav_df"].to_dict("records")
            events = result["events_df"].to_dict("records")
            key = dict(scope=scope, initial_capital=cap)
            navs.extend({**key, **m} for m in marks)
            fills.extend({**key, **m} for m in events)
            print(scope, cap, len(marks), len(events), marks[-1]["nav"], flush=True)
    write_csv(out / (prefix + "reference_nav.csv"), navs)
    write_csv(out / (prefix + "reference_fills.csv"), fills)


def verify(out, primary, board_lots):
    prefix = "lot_" if board_lots else ""
    marks = read_csv(out / (prefix + "reference_nav.csv"))
    fills = read_csv(out / (prefix + "reference_fills.csv"))
    nav = read_csv(primary / "nav.csv")
    events = read_csv(primary / "events.csv")
    trades = read_csv(primary / "trades.csv")
    spec = read_csv(primary / "summary.csv")
    error = defaultdict(float)

    def close(a, b, key):
        diff = abs(float(a) - float(b))
        error[key] = max(error[key], diff)
        if diff > 1e-6:
            raise AssertionError((key, a, b, diff))

    accounts = []
    for scope in dict.fromkeys(row["scope"] for row in marks):
        for capital in CAPITALS:

            def sel(row):
                return row["scope"] == scope and int(row["initial_capital"]) == capital

            m = list(filter(sel, marks))
            n = list(filter(sel, nav))
            f = list(filter(sel, fills))
            e = [
                r
                for r in events
                if sel(r) and (r["action"] == "BUY" or r["action"].startswith("SELL"))
            ]
            (metrics,) = [r for r in spec if sel(r)]
            amount = 0
            for a, b in zip(m, n, strict=True):
                assert a["date"] == b["date"]
                for k in ("nav", "cash", "market_value", "pos_count", "utilization"):
                    close(a[k], b[k], k)
            opened = {}
            closed = [r for r in trades if sel(r)]
            exits = iter(closed)
            daily_realized = defaultdict(float)
            for a, b in zip(f, e, strict=True):
                assert (a["date"], a["code"], a["action"]) == (
                    b["date"],
                    b["code"],
                    b["action"],
                ), (
                    scope,
                    capital,
                    a,
                    b,
                )
                for k in ("price", "shares"):
                    close(a[k], b[k], k)
                c = a["code"]
                commission = 10 + float(a["price"]) * float(a["shares"]) * 0.0005
                amount += commission
                close(commission, b["cost"], "fee")
                if a["action"] == "BUY":
                    assert c not in opened
                    opened[c] = (a, commission)
                else:
                    entry, entry_fee = opened.pop(c)
                    t = next(exits)
                    assert (
                        t["code"],
                        t["entry_day"],
                        t["exit_day"],
                        t["exit_reason"],
                    ) == (c, entry["date"], a["date"], a["action"])
                    assert t["is_sale"] == "1"
                    for k, key in (
                        ("entry_raw_open", "price"),
                        ("exit_price", "price"),
                        ("shares_at_exit", "shares"),
                    ):
                        ref = entry if k == "entry_raw_open" else a
                        close(ref[key], t[k], k)
                    gross_entry = float(entry["price"]) * float(entry["shares"])
                    close(
                        gross_entry / float(a["shares"]),
                        t["adjusted_entry"],
                        "adjusted_entry",
                    )
                    close(gross_entry + entry_fee, t["entry_debit"], "entry_debit")
                    close(
                        float(a["price"]) * float(a["shares"]) - commission,
                        t["exit_proceeds"],
                        "exit_proceeds",
                    )
                    close(commission, t["exit_cost"], "exit_cost")
                    realized = float(a["pnl"]) - entry_fee
                    close(realized, b["pnl"], "realized per lot")
                    close(realized, t["net"], "trade net")
                    daily_realized[a["date"]] += realized
            assert next(exits, None) is None
            assert len(opened) == int(n[-1]["pos_count"])
            cumulative = 0.0
            for a, b in zip(m, n, strict=True):
                cumulative += daily_realized[a["date"]]
                close(cumulative, b["realized"], "realized")
                close(
                    float(a["nav"]) - capital - cumulative,
                    b["unrealized"],
                    "unrealized",
                )
            high = capital
            dd = 0
            for day in m:
                v = float(day["nav"])
                high = max(high, v)
                dd = min(dd, 100 * (v / high - 1))
            close(dd, metrics["max_dd_pct"], "max_dd_pct")
            close(m[-1]["nav"], metrics["nav"], "end_nav")
            close(amount, metrics["cost_total"], "cost_total")
            accounts.append(
                dict(
                    scope=scope,
                    initial_capital=capital,
                    nav_obs=len(n),
                    fills=len(f),
                    round_trips=len(closed),
                    nav=float(m[-1]["nav"]),
                    max_dd_pct=dd,
                )
            )
    write_json(
        out / (prefix + "reconciliation.json"),
        dict(
            accounts=accounts,
            counts=dict(
                nav_obs=sum(x["nav_obs"] for x in accounts),
                fills=sum(x["fills"] for x in accounts),
                round_trips=sum(x["round_trips"] for x in accounts),
            ),
            max_abs_diff=dict(error),
            calculation=(
                "Original attached account using independent OHLC, ATR and starting portfolios."
            ),
        ),
    )
    print(accounts)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("prices", "simulate", "reconcile"))
    for k in ("project", "ranks", "attachment", "verified", "primary", "out", "board-lots"):
        p.add_argument("--" + k, type=Path, required=(k == "out"))
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.command == "prices":
        market_inputs(args.project.resolve(), args.ranks, args.out)
    elif args.command == "simulate":
        replay(
            args.project.resolve(),
            args.ranks,
            args.out,
            args.verified,
            args.attachment,
            args.board_lots,
        )
    else:
        verify(args.out, args.primary, args.board_lots)
