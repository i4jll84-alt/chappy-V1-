"""One retrospective replay on every session of the sealed November swing-agent replay."""

import argparse
import csv
import hashlib
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from trading_system.research import swing_forward as sf
from trading_system.research import swing_macro as macro
from trading_system.research import swing_nikkei as nk
from trading_system.research.daytrade_universe import dated_inputs

ROOT = Path.cwd()
SOURCE = ROOT / "data/offline-verification/swing-agent-replay-20261113"
OUT = ROOT / "data/offline-verification/swing-market-replay-20261113"
HISTORY_CUT = datetime.fromisoformat("2026-11-13T17:20:50+09:00")


def read(p):
    return json.loads(p.read_bytes())


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def retrieve(client, url, file):
    if not file.exists():
        response = client.get(url)
        response.raise_for_status()
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("xb") as f:
            f.write(response.content)
        save(
            file.with_suffix(file.suffix + ".json"),
            dict(
                url=url,
                observed_at=datetime.now(sf.JST).isoformat(),
                sha256=hashlib.sha256(response.content).hexdigest(),
            ),
        )
    body = file.read_bytes()
    meta = read(file.with_suffix(file.suffix + ".json"))
    if meta["url"] != url or meta["sha256"] != hashlib.sha256(body).hexdigest():
        raise ValueError("raw_source_mismatch")
    return body


def records():
    return {p.stem: read(p) for p in sorted((SOURCE / "audit").glob("Set*.json"))}


def prepare():
    records_by_id = records()
    buys = read(SOURCE / "agent-orders.json")
    if set(records_by_id) != set(buys) or len(buys) != 7:
        raise ValueError("unexpected_sealed_sessions")
    raw = OUT / "raw"
    packs = {}
    with httpx.Client(timeout=25) as client:
        urls = {"JP10Y": macro.MOF_HISTORY, "USDJPY": macro.ECB}
        for k in ("SP500", "NASDAQCOM", "DGS10"):
            urls[k] = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + k + "&cosd=2026-10-01"
        series = {k: retrieve(client, url, raw / k) for k, url in urls.items()}
        series = {k: list(macro.observations(k, b)) for k, b in series.items()}
        series["JP10Y"].extend(
            macro.observations("JP10Y", retrieve(client, macro.MOF, raw / "JP10Y-current"))
        )
        reports = nk.listed_reports(client, date(2026, 11, 10))
        oldest = min(date.fromisoformat(r["decision"]) for r in records_by_id.values())
        if not any(d < oldest for d in reports):
            reports.update(nk.listed_reports(client, oldest - timedelta(days=1)))
        for tag, rec in records_by_id.items():
            entry = date.fromisoformat(rec["entry"])
            prev = date.fromisoformat(rec["decision"])
            point = datetime.combine(entry, time(7, 55), sf.JST)
            limits = {"JP10Y": prev - timedelta(days=1), "USDJPY": entry - timedelta(days=1)}
            us = entry
            for _ in range(3):
                us -= timedelta(days=1)
                while us.weekday() >= 5:
                    us -= timedelta(days=1)
            frozen = {}
            for k, observations in series.items():
                cutoff = limits.get(k, us)
                frozen[k] = macro.summarize_observations(
                    k, ((d, v) for d, v in observations if d <= cutoff), point
                )
            report = max(d for d in reports if d < prev)
            blob = retrieve(client, reports[report], raw / (str(report) + ".zip"))
            frozen["NK225F"] = nk.read_pdf(nk.extract_pdf(blob, report), report, entry)
            by_code = {r["code"]: r for r in rec["machine_rows"]}
            groups = {}
            for arm, orders in buys[tag].items():
                rows = {}
                for alias in orders["buy"]:
                    r = by_code[rec["ids"][alias]]
                    rows[alias] = {
                        k: r[k]
                        for k in (
                            "return20",
                            "close",
                            "sma5",
                            "sma20",
                            "turnover",
                            "prior_turnover_mean",
                            "plus3_exclusions",
                        )
                    }
                groups[arm] = rows
            packs[tag] = dict(asof=str(point), groups=groups, market=frozen)
    save(OUT / "inputs-v2.json", packs)
    save(
        OUT / "plan-v2.json",
        dict(
            sessions=sorted(packs),
            source="sealed seven-session agent replay",
            capital=2000000,
            lot=100,
            fee=10,
            slippage="0.05%",
            reconstructed=True,
            inputs_sha256=hashlib.sha256((OUT / "inputs-v2.json").read_bytes()).hexdigest(),
            lag=(
                "US three weekdays; ECB prior date; JP yield strictly before selection date; "
                "OSE report strictly before selection date"
            ),
        ),
    )


def evaluate(sequence="batch"):
    prefix = "chronological-" if sequence == "chronological" else ""
    answer_file = OUT / (prefix + "decisions.json")
    packs = read(OUT / "inputs-v2.json")
    plan = read(OUT / "plan-v2.json")
    if hashlib.sha256((OUT / "inputs-v2.json").read_bytes()).hexdigest() != plan["inputs_sha256"]:
        raise ValueError("input_seal_mismatch")
    answer = read(answer_file)
    if set(answer) != set(packs):
        raise ValueError("incomplete_decisions")
    orders = read(SOURCE / "agent-orders.json")
    recs = records()
    with (SOURCE / "daily-nav.csv").open(encoding="utf-8-sig") as f:
        reference = {(r["entry"], r["account"]): r for r in csv.DictReader(f)}
    books = {
        (arm, v): sf.initial()["agent_" + arm]
        for arm in ("current", "plus3")
        for v in ("base", "macro")
    }
    rows = []
    for tag in sorted(recs, key=lambda t: recs[t]["entry"]):
        record = recs[tag]
        day = record["entry"]
        cols, _, _ = dated_inputs(ROOT / "data/jquants", [day], HISTORY_CUT)
        quotes = {c: bars[0] for c, bars in cols.items()}
        for (arm, variant), book in books.items():
            base = orders[tag][arm]
            defer = answer[tag][arm]["defer"]
            review = answer[tag][arm]
            context = {"manifest_sha256": plan["inputs_sha256"], "series": packs[tag]["market"]}
            issue = macro.validate_assessment(
                context, dict(review, manifest_sha256=plan["inputs_sha256"]), base["buy"]
            )
            if issue:
                raise ValueError((tag, arm, issue))
            ids = record["ids"]
            chosen = {
                "buy": [ids[c] for c in base["buy"] if variant == "base" or c not in defer],
                "sell": [ids.get(c, c) for c in base["sell"] if ids.get(c, c) in book["positions"]],
            }
            if any(c in book["positions"] for c in chosen["buy"]):
                raise ValueError("duplicate_position")
            state, fills, metrics = sf.settle_account(book, chosen, quotes, day, record["exit"])
            if state["status"] != "ok" or not metrics:
                raise ValueError("unresolved_valuation")
            books[arm, variant] = state
            if variant == "base" and any(
                Decimal(metrics[k]) != Decimal(reference[day, "agent_" + arm][k])
                for k in ("nav", "net", "realized", "unrealized", "fees", "slippage", "max_dd_pct")
            ):
                raise ValueError("baseline_reconciliation_failed")
            rows.append(
                dict(
                    day=day,
                    rule=arm,
                    variant=variant,
                    positions=len(state["positions"]),
                    defer=len(defer) if variant == "macro" else 0,
                    bought=sum(f["status"] == "simulated" and f["action"] == "buy" for f in fills),
                    sold=sum(f["status"] == "simulated" and f["action"] == "sell" for f in fills),
                    **metrics,
                )
            )
    totals = []
    for arm, v in books:
        sub = [r for r in rows if r["rule"] == arm and r["variant"] == v]
        totals.append(
            dict(
                sub[-1],
                **{f"total_{k}": sum(r[k] for r in sub) for k in ("defer", "bought", "sold")},
            )
        )
    save(
        OUT / (prefix + "results.json"),
        dict(
            days=rows,
            summary=totals,
            decisions_sha256=hashlib.sha256(answer_file.read_bytes()).hexdigest(),
        ),
    )
    print(json.dumps(totals))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "evaluate"])
    parser.add_argument("--sequence", choices=["batch", "chronological"], default="batch")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    else:
        evaluate(args.sequence)
