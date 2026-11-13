"""Offline extension of the November replay, plus a separately labelled US-only sensitivity."""

import hashlib
import json
import sqlite3
from datetime import date, datetime, time, timedelta
from decimal import Decimal as D

from swing_short_replay import ASOF, ROOT, THROUGH, reverse_fee_table, run_book

from trading_system.research import swing_forward as sf
from trading_system.research import swing_macro as macro
from trading_system.research import swing_nikkei as nk
from trading_system.research import swing_shorts as short
from trading_system.research.daytrade_universe import dated_inputs
from trading_system.research.swing_credit import tokyo
from trading_system.research.swing_short_sources import SHORT_PAGE, PublicInputs
from trading_system.research.swing_windows import history_rows

BASE = ROOT / "data/offline-verification"
MARKET = BASE / "swing-market-replay-20261113"
OUT = BASE / "swing-short-extension-20261113"
CLOCK = "08:15"
FIRST = "2026-10-01"
FIELDS = ["Date", "Code", "C", "O", "Va", "AdjFactor", "ExRT"]


def checked(path, digest):
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != digest:
        raise ValueError(f"hash mismatch: {path.name}")
    return body


def catalogue(db, root, ep, cols, lo, hi, refs, group="Date"):
    by_day = {}
    for row in history_rows(db, root, ep, cols, lo, hi, refs):
        if not row[group]:
            continue
        rows = by_day.setdefault(row[group], {})
        if row["Code"] in rows:
            previous = rows[row["Code"]]
            if group != "PubDate" or previous["Date"] == row["Date"]:
                raise ValueError("duplicate code and observation date")
            if previous["Date"] > row["Date"]:
                continue
        rows[row["Code"]] = row
    return by_day


def get_history():
    jq = ROOT / "data/jquants"
    archive = jq / "history"
    wal = archive / "history.sqlite-wal"
    if wal.exists() and wal.stat().st_size:
        raise ValueError("active history writer")
    refs = []
    with sqlite3.connect(
        (archive / "history.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True
    ) as db:
        sessions = [
            r["Date"]
            for r in history_rows(
                db,
                archive,
                "/markets/calendar",
                ["Date", "HolDiv"],
                "2026-08-01",
                "2026-11-30",
                refs,
            )
            if r["HolDiv"] in ("1", "2")
        ]
        sessions = sorted(sessions)
        start = sessions[sessions.index(FIRST) - 21]
        quotes = catalogue(db, archive, "/equities/bars/daily", FIELDS, start, "2026-10-30", refs)
        masters = catalogue(
            db,
            archive,
            "/equities/master",
            ["Code", "Date", "ProdCat", "Mkt", "MrgnNm"],
            sessions[sessions.index(FIRST) - 1],
            "2026-10-30",
            refs,
        )
        exchanges = catalogue(
            db,
            archive,
            "/markets/margin-interest",
            ["Code", "Date", "PubDate", "LongVol", "ShrtVol"],
            "2026-09-25",
            "2026-10-29",
            refs,
            "PubDate",
        )
    for d in sessions:
        if "2026-11-02" <= d <= THROUGH:
            q, master, source = dated_inputs(jq, [d], ASOF)
            quotes[d] = {c: bars[0] for c, bars in q.items()}
            masters[d] = master
            refs.extend(source)
    return sessions, quotes, masters, exchanges, refs


def historical_jsf(day):
    root = ROOT / "data/free-market/jsf-history"
    rec = sf.read(root / "days" / (day + ".json"))
    if rec["date"] != day or rec["state"] != "checked":
        raise ValueError("wrong JSF date")
    rows = tokyo(checked(root / rec["csv"]["path"], rec["csv"]["sha256"]))
    for r in rows.values():
        for src, dst in [("貸株残高（株）", "貸株残高株数"), ("融資残高（株）", "融資残高株数")]:
            v = r[src].replace(",", "")
            r[dst] = v if v.isdigit() else None
    return rows, dict(date=day, sha256=rec["csv"]["sha256"], available_at=rec["available_at"])


def raw_market():
    raw = MARKET / "raw"
    series = {}
    for k in ("SP500", "NASDAQCOM", "USDJPY", "DGS10", "JP10Y", "JP10Y-current"):
        path = raw / k
        meta = sf.read(raw / (k + ".json"))
        key = k.split("-")[0]
        series.setdefault(key, []).extend(macro.observations(key, checked(path, meta["sha256"])))
    reports = {}
    for p in raw.glob("*.zip"):
        d = date.fromisoformat(p.stem)
        reports[d] = nk.extract_pdf(checked(p, sf.read(p.with_suffix(".zip.json"))["sha256"]), d)
    return series, reports


def market_on(day, prior, series, reports):
    entry = date.fromisoformat(day)
    when = datetime.combine(entry, time(7, 55), sf.JST)
    us = entry
    for _ in range(3):
        us -= timedelta(days=1)
        while us.weekday() >= 5:
            us -= timedelta(days=1)
    result = {}
    for k, values in series.items():
        last = {
            "USDJPY": entry - timedelta(days=1),
            "JP10Y": date.fromisoformat(prior) - timedelta(days=1),
        }.get(k, us)
        try:
            result[k] = macro.summarize_observations(
                k, ((d, v) for d, v in values if d <= last), when
            )
        except ValueError as exc:
            if str(exc) != "insufficient_observations":
                raise
            result[k] = {"state": "unavailable"}
    previous = [d for d in reports if str(d) < prior]
    if previous:
        last = max(previous)
        result["NK225F"] = nk.read_pdf(reports[last], last, entry)
    else:
        result["NK225F"] = {"state": "unavailable"}
    return result


def us_check(market):
    us = [market[k] for k in ("SP500", "NASDAQCOM")]
    if any(v["state"] != "usable" for v in us):
        return "market_data_missing"
    return "pass" if all(D(v["change"]) < 0 for v in us) else "market_not_bearish"


def historical_packet(charts, entry, window, loans, exchanges, market, public):
    # Historical search results identify a requested day's TSE-only status and balances.
    entry_rows = loans[entry]
    suffix = window[-1].replace("-", "") + "_Stocks_Restricted_Nextday.csv"
    triggers = next(public.files(SHORT_PAGE + "%/" + suffix), None)
    if triggers is None or any(
        len(r) < 2 or not r[0].isalnum() or len(r[0]) != 4 for r in triggers if r
    ):
        raise ValueError("missing historical JPX restriction list: " + entry)
    flagged = {r[0] + "0" for r in triggers if r}
    # Apply the entry-day status only as a retrospective availability screen.
    allowed = {
        c for c, r in entry_rows.items() if r["貸借区分"] == "貸借" and r["制限措置等"] == ""
    }
    pool = [r for r in charts if r["code"] in allowed - flagged][:5]
    dates = window[-3:-1][
        ::-1
    ]  # latest normal confirmed JSF data is one session before the selection day
    publications = window[-2:][::-1]
    exchange = [exchanges.get(d, {}) for d in publications]
    for row in pool:
        row["credit"] = short.credit_details(row, loans, dates, exchange, publications)
    return dict(state="retrospective_TSE", candidates=pool, market_state=us_check(market))


def summary(book):
    v = book["final"]

    def pnl(row):
        return D(str(row["before_reverse"])) - D(str(row["known_reverse"]))

    closed = sum((pnl(lot) for lot in book["closed_trades"]), D(0))
    high = D(2000000)
    maxdd = D(0)
    for row in book["daily"]:
        nav = D(2000000) + pnl(row)
        high = max(high, nav)
        maxdd = max(maxdd, (1 - nav / high) * 100)
    return dict(
        trades=v["closed"] + v["open"],
        closed=v["closed"],
        open=v["open"],
        provisional_yen=pnl(v),
        closed_yen=closed,
        open_yen=pnl(v) - closed,
        missing_fee_days=v["missing_reverse_stock_days"],
        max_drawdown_pct=maxdd,
        capital_return_pct=pnl(v) / 20000,
        skips=book["skips"],
    )


def simulate(packets, quotes, cal, reverse, matched=False):
    span = {d: quotes[d] for d in cal if min(packets) <= d <= THROUGH}
    return {
        arm: run_book(arm, CLOCK, cal, packets, span, reverse, matched)
        for arm in ("chart", "market")
    }


def main():
    cal, quotes, masters, exchanges, refs = get_history()
    series, reports = raw_market()
    old = sf.read(BASE / "swing-short-20261113/result.json")
    old_packets = sf.read(BASE / "swing-short-20261113/signals.json")
    if sf.signature(old_packets) != old["inputs_sha256"]:
        raise ValueError("previous input seal mismatch")
    old_market = json.loads(
        checked(MARKET / "inputs-v2.json", sf.read(MARKET / "plan-v2.json")["inputs_sha256"])
    )
    market_by_day = {p["asof"][:10]: p["market"] for p in old_market.values()}
    strict = {}
    supplement = {}
    coverage = {}
    reverse, fee_refs = reverse_fee_table()
    public = PublicInputs(ROOT, ASOF)
    try:
        stocks = {}
        for d in cal:
            if "2026-09-25" <= d <= "2026-10-30":
                stocks[d], meta = historical_jsf(d)
                refs.append(meta)
                for code, r in stocks[d].items():
                    fee = r["品貸料率（円）"].strip()
                    if fee and (not fee.strip("*＊") or fee.replace(".", "", 1).isdigit()):
                        reverse.setdefault((d, code), D(0) if not fee.strip("*＊") else D(fee))
        for i, d in enumerate(cal):
            if not FIRST <= d <= THROUGH:
                continue
            window = cal[i - 21 : i]
            prev = window[-1]
            market = market_on(d, prev, series, reports)
            expected_report = window[-2]
            if market["NK225F"].get("date") != expected_report:
                market["NK225F"] = {"state": "unavailable", "missing_report": expected_report}
            if d in old_packets:
                if market != market_by_day[d]:
                    raise ValueError("market replay mismatch " + d)
                packet = old_packets[d][CLOCK]
                strict[d] = {CLOCK: packet}
            else:
                charts = short.chart_rows(quotes, masters[prev], window)
                if d < "2026-11-02":
                    packet = historical_packet(charts, d, window, stocks, exchanges, market, public)
                else:
                    cutoff = datetime.fromisoformat(d + "T08:15:00+09:00")
                    if d > "2026-11-02":
                        selection_cut = datetime.combine(
                            date.fromisoformat(prev), time(23, 59, 59), sf.JST
                        )
                        frozen = sf.screen(ROOT, date.fromisoformat(prev), selection_cut)
                        if frozen["window"] != window or frozen["short_charts"] != charts:
                            raise ValueError("archived and cutoff-qualified charts differ")
                    packet = short.prepare(
                        ROOT, d, window, charts, {"series": market}, cutoff, reconstructed=True
                    )
                    strict[d] = {CLOCK: packet}
            coverage[d] = dict(
                us=us_check(market),
                original_market=short.market_check({"series": market}),
                futures_date=market["NK225F"].get("date"),
                futures_state=market["NK225F"]["state"],
                expected_futures_date=expected_report,
                source=packet["state"],
                slots=len(packet["candidates"]),
            )
            supplement[d] = {CLOCK: dict(packet, market_state=us_check(market))}
    finally:
        public.db.close()
    # Verify the old seven entry dates with the very same valuation horizon and fee cut.
    old_reverse, _ = reverse_fee_table()
    for arm, book in simulate(
        {d: strict[d] for d in old_packets}, quotes, cal, old_reverse
    ).items():
        if any(
            D(str(book["final"][k])) != D(str(old["books"][CLOCK][arm]["final"][k]))
            for k in ("before_reverse", "known_reverse", "closed", "open")
        ):
            raise ValueError("previous result not reproduced")
    start = next(d for d in sorted(supplement) if coverage[d]["us"] != "market_data_missing")
    usable = {d: v for d, v in supplement.items() if d >= start}
    main_books = simulate(strict, quotes, cal, old_reverse)
    prior_close_cut = simulate(
        {d: v for d, v in strict.items() if d > "2026-11-02"}, quotes, cal, old_reverse
    )
    strict_common = simulate(strict, quotes, cal, old_reverse, True)
    compared = simulate(usable, quotes, cal, reverse)
    matched = simulate(usable, quotes, cal, reverse, True)
    runs = {
        "original": main_books,
        "original_prior_close_cut": prior_close_cut,
        "original_common_input": strict_common,
        "us_sensitivity": compared,
        "us_common_input": matched,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    result = dict(
        through=THROUGH,
        original_first=min(strict),
        original_days=len(strict),
        sensitivity_first=start,
        sensitivity_days=len(usable),
        coverage=coverage,
        previous_reproduced=True,
        summary={name: {a: summary(v) for a, v in arms.items()} for name, arms in runs.items()},
    )
    for name, value in (
        ("summary", result),
        ("books", runs),
        ("signals", dict(original=strict, us=usable)),
        ("sources", dict(archive=refs, jpx=public.refs, fees=fee_refs)),
    ):
        sf.write(OUT / (name + ".json"), json.loads(json.dumps(value, default=str)))
    print(json.dumps({k: v for k, v in result.items() if k != "coverage"}, default=str))


if __name__ == "__main__":
    main()
