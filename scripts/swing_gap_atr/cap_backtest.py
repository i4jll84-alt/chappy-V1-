"""Continuous paper accounts with fixed entry ranking, costs, six-session expiry and cap.

Run with the V1 Python; --project supplies the read-only market data and pinned research modules.
"""

import argparse
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal as D
from fractions import Fraction
from pathlib import Path
from statistics import mean, stdev

START = D(2_000_000)
FEE = D(10)
SLIP = D("0.0005")
SHARES = D(100)
TOL = D("1e-11")
# Code, ex-date -> (preannounced shares after / before, official notice).
KNOWN = {
    ("165A0", "2024-09-27"): ("8", "https://www.sbigroup.co.jp/news/pdf/group/2024/0821_a.pdf"),
    ("23230", "2026-01-28"): (
        "3",
        "https://www.fonfun.co.jp/wp-content/uploads/2026/01/2026011401.pdf",
    ),
    ("60310", "2023-12-28"): ("2", "https://www2.jpx.co.jp/disc/60310/140120231110586263.pdf"),
    ("55270", "2025-07-30"): ("3", "https://www2.jpx.co.jp/disc/55270/140120250714514214.pdf"),
}


def positive(p):
    if p is None or p == "":
        return None
    return D(str(p)) if D(str(p)) > 0 else None


def costs(notional):
    return FEE + notional * SLIP


@dataclass
class Lot:
    entered: str
    due: str
    entry: D
    atr: D
    stop: D
    shares: D
    debit: D
    rank: int
    quote: D


@dataclass
class Account:
    policy: str
    cap: int | None
    scope: str
    cash: D = START
    realized: D = D(0)
    positions: dict = field(default_factory=dict)
    nav: list = field(default_factory=list)
    events: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    exceptions: list = field(default_factory=list)

    def ident(self):
        return dict(scope=self.scope, policy=self.policy, cap=self.cap or "none")

    def event(self, day, code, action, **kwargs):
        self.events.append(dict(**self.ident(), date=day, code=code, action=action, **kwargs))

    def issue(self, day, code, kind, strict, **kwargs):
        self.exceptions.append(
            dict(**self.ident(), date=day, code=code, kind=kind, strict_break=strict, **kwargs)
        )

    def settle_lot(self, day, code, action, px, *, writeoff=False):
        p = self.positions.pop(code)
        gross = px * p.shares
        cost = D(0) if writeoff else costs(gross)
        self.cash += gross - cost
        net = gross - cost - p.debit
        self.realized += net
        self.event(day, code, action, price=px, shares=p.shares, cost=cost, pnl=net, rank=p.rank)
        self.trades.append(
            dict(
                **self.ident(),
                entry_day=p.entered,
                exit_day=day,
                code=code,
                entry_rank=p.rank,
                entry_raw_open=p.quote,
                adjusted_entry=p.entry,
                shares_at_exit=p.shares,
                exit_price=px,
                exit_reason=action,
                is_sale=int(not writeoff),
                entry_debit=p.debit,
                exit_proceeds=gross - cost,
                net=net,
                exit_cost=cost,
            )
        )

    def snapshot(self, day, mkt_value, n_cap, cap_only, high, n_missing=0):
        equity = self.cash + mkt_value
        self.nav.append(
            dict(
                **self.ident(),
                date=day,
                nav=equity,
                cash=self.cash,
                market_value=mkt_value,
                pos_count=len(self.positions),
                utilization=mkt_value / equity if equity else None,
                skipped_by_cap=n_cap,
                cap_only=cap_only,
                peak_positions=high,
                realized=self.realized,
                unrealized=equity - START - self.realized,
                discrepancy_events=n_missing,
            )
        )


def ratio_for(day, code, bar):
    f = positive(bar.get("AdjFactor"))
    ex = bar.get("ExRT")
    if f == 1 and ex in (None, ""):
        return D(1), "clean"
    if f is None or f == 1:
        return D(1), "unclassified_right"
    if ex not in ("1", "2", 1, 2):
        return D(1), "unclassified_factor"
    if (code, day) in KNOWN:
        ratio = D(KNOWN[code, day][0])
        assert abs(f * ratio - 1) <= D("0.00002"), (day, code, f, ratio)
        return ratio, "verified"
    frac = Fraction(1 / f).limit_denominator(1000)
    simple = D(frac.numerator) / frac.denominator
    if abs(f * simple - 1) <= D("0.00001"):
        return simple, "observed_factor"
    return 1 / f, "observed_factor"


def reference_list(market, day, names):
    quote = market.bars(day)
    assert len(names) <= 5 and len(set(names)) == len(names)
    return [
        dict(code=c, rank=j, prev_close=positive(quote.get(c, {}).get("C")), atr=market.atr(day, c))
        for j, c in enumerate(names, 1)
    ]


def order_values(row, bar, day):
    op = positive(bar.get("O"))
    if op is None:
        return ("no_open", None, None, None)
    close = row["prev_close"]
    atr = row["atr"]
    if close is None:
        return ("no_reference", op, None, None)
    ratio, kind = ratio_for(day, row["code"], bar)
    if kind == "verified":
        close /= ratio
        if atr is not None:
            atr /= ratio
    elif kind != "clean":
        return ("unverified_entry_right", op, None, None)
    if op > close * D("1.03"):
        return ("gap", op, None, None)
    if atr is None:
        return ("no_atr", op, None, None)
    return ("ok", op, atr, SHARES * op + costs(SHARES * op))


def run_day(acct, day, due_day, picks, bars, sf):
    cash_before = acct.cash
    opening_held = set(acct.positions)
    start_issues = len(acct.exceptions)
    sold = set()
    for c, p in acct.positions.items():
        bar = bars.get(c)
        if bar is None:
            continue
        ratio, kind = ratio_for(day, c, bar)
        if kind.startswith("unclassified"):
            acct.issue(day, c, kind, True, adj_factor=bar.get("AdjFactor"), ex_rt=bar.get("ExRT"))
        elif ratio != 1:
            p.shares *= ratio
            p.entry /= ratio
            p.atr /= ratio
            p.stop /= ratio
            acct.event(
                day,
                c,
                "ADJUST",
                shares=p.shares,
                price=p.entry,
                ratio=ratio,
                reason=kind,
                adj_factor=bar.get("AdjFactor"),
                ex_rt=bar.get("ExRT"),
            )
            acct.issue(
                day,
                c,
                "holding_adjustment_" + kind,
                kind != "verified",
                ratio=ratio,
                shares=p.shares,
            )
            if p.shares % 100:
                acct.issue(day, c, "non_round_lot", True, shares=p.shares)
    for c, p in list(acct.positions.items()):
        if day >= p.due:
            op = positive(bars.get(c, {}).get("O"))
            if (
                op is None
                and acct.scope == "history_zero_recovery"
                and (c, day) == ("14000", "2024-01-10")
            ):
                acct.issue(day, c, "writeoff_zero_recovery", True, shares=p.shares)
                acct.settle_lot(day, c, "WRITEOFF_NO_SALE", D(0), writeoff=True)
                sold.add(c)
                continue
            if op is None:
                acct.issue(day, c, "expiry_fallback_entry", True, price=p.entry, shares=p.shares)
                op = p.entry
            if p.shares % 100:
                acct.issue(day, c, "odd_lot_model_sale", True, shares=p.shares, price=op)
            acct.settle_lot(day, c, "SELL_TIME", op)
            sold.add(c)
    n_cap = cap_only = 0
    spent = D(0)
    reserved = None
    if acct.policy == "reserved":
        state = dict(
            cash=str(cash_before), positions={c: {"exit": day} for c in opening_held}, status="ok"
        )
        # At the previous close, reserve V1 estimated debits in fixed rank order.
        eligible = [
            dict(code=r["code"], estimated_debit=str(sf.cost(r["prev_close"])))
            for r in picks
            if r["prev_close"] is not None and r["atr"] is not None
        ]
        reserved = sf.account_orders(state, eligible, day)
    for r in picks:
        c = r["code"]
        if c in acct.positions or c in sold:
            reason = "sold_today" if c in sold else "already_held"
            acct.event(day, c, "SKIP", rank=r["rank"], reason=reason)
            continue
        if reserved is not None and c not in reserved["buy"]:
            acct.event(
                day, c, "SKIP", rank=r["rank"], reason=reserved["skips"].get(c, "no_prior_features")
            )
            continue
        reason, op, atr, amount = order_values(r, bars.get(c, {}), day)
        allowed = cash_before - spent if acct.policy == "morning" else acct.cash
        if acct.cap is not None and len(acct.positions) >= acct.cap:
            n_cap += 1
            only = reason == "ok" and amount <= allowed
            cap_only += int(only)
            acct.event(
                day,
                c,
                "SKIP",
                rank=r["rank"],
                reason="cap",
                solely_cap=int(only),
                other_reason=("ok" if only else ("cash" if reason == "ok" else reason)),
            )
            continue
        if reason != "ok" or amount > allowed:
            if reason == "unverified_entry_right":
                acct.issue(
                    day,
                    c,
                    reason,
                    True,
                    adj_factor=bars[c].get("AdjFactor"),
                    ex_rt=bars[c].get("ExRT"),
                )
            acct.event(day, c, "SKIP", rank=r["rank"], reason=reason if reason != "ok" else "cash")
            continue
        spent += amount
        acct.cash -= amount
        assert acct.cash >= 0 and (acct.policy != "morning" or spent <= cash_before)
        acct.positions[c] = Lot(day, due_day, op, atr, op - 2 * atr, SHARES, amount, r["rank"], op)
        acct.event(
            day,
            c,
            "BUY",
            rank=r["rank"],
            price=op,
            shares=SHARES,
            cost=costs(op * SHARES),
            stop=op - 2 * atr,
            previous_close=r["prev_close"],
            atr=atr,
            debit=amount,
            cash_before_sales=cash_before,
            spent_at_open=spent,
        )
    high = len(acct.positions)
    for c, p in list(acct.positions.items()):
        bar = bars.get(c, {})
        op = positive(bar.get("O"))
        lo = positive(bar.get("L"))
        if op is None or lo is None:
            acct.issue(
                day,
                c,
                "stop_observation_missing",
                True,
                no_row=int(not bar),
                open_missing=int(op is None),
                low_missing=int(lo is None),
            )
            continue
        if day > p.entered and op <= p.stop:
            px, kind = op, "SELL_STOP_OPEN"
        elif lo <= p.stop:
            px, kind = p.stop, "SELL_STOP_INTRADAY"
        else:
            continue
        if p.shares % 100:
            acct.issue(day, c, "odd_lot_model_sale", True, shares=p.shares, price=px)
        acct.settle_lot(day, c, kind, px)
    marked = D(0)
    unrealized = D(0)
    for c, p in acct.positions.items():
        cl = positive(bars.get(c, {}).get("C"))
        if cl is None:
            acct.issue(day, c, "mark_fallback_entry", True, price=p.entry, shares=p.shares)
            cl = p.entry
        marked += cl * p.shares
        unrealized += cl * p.shares - p.debit
    acct.snapshot(day, marked, n_cap, cap_only, high, len(acct.exceptions) - start_issues)
    assert abs(acct.nav[-1]["nav"] - (START + acct.realized + unrealized)) < TOL


def metrics(acct, through, period):
    rows = [r for r in acct.nav if r["date"] <= through]
    if not rows:
        return None
    last, first = rows[-1], rows[0]
    high = START
    dd = D(0)
    returns = []
    prev = START
    for i, r in enumerate(rows):
        nav = r["nav"]
        high = max(high, nav)
        dd = min(dd, nav / high - 1)
        if i:
            returns.append(float(nav / prev - 1))
        prev = nav
    net = last["nav"] - START
    cagr = float(last["nav"] / START) ** (252 / len(rows)) - 1
    counters = Counter(e["action"] for e in acct.events if e["date"] <= through)
    skips = Counter(
        e["reason"] for e in acct.events if e["action"] == "SKIP" and e["date"] <= through
    )
    hits = Counter(e["kind"] for e in acct.exceptions if e["date"] <= through)
    return dict(
        **acct.ident(),
        period=period,
        first_mark=first["date"],
        last_mark=last["date"],
        days=len(rows),
        nav=last["nav"],
        net=net,
        total_return_pct=net * 100 / START,
        cagr_pct=cagr * 100,
        annual_return_intervals=len(returns),
        cagr_intervals_pct=100 * (float(last["nav"] / START) ** (252 / len(returns)) - 1)
        if returns
        else None,
        max_dd_pct=dd * 100,
        calmar=cagr / abs(float(dd)) if dd else None,
        sharpe=mean(returns) / stdev(returns) * math.sqrt(252)
        if len(returns) > 1 and stdev(returns)
        else None,
        mean_positions=mean([r["pos_count"] for r in rows]),
        max_positions=max(r["pos_count"] for r in rows),
        peak_intraday_positions=max(r["peak_positions"] for r in rows),
        avg_utilization_pct=mean([r["utilization"] for r in rows]) * 100,
        buys=counters["BUY"],
        sells=sum(v for k, v in counters.items() if k.startswith("SELL")),
        open_positions=last["pos_count"],
        cap_skips=skips["cap"],
        cap_only=sum(r["cap_only"] for r in rows),
        held_skips=skips["already_held"],
        sold_today_skips=skips["sold_today"],
        gap_skips=skips["gap"],
        no_open_skips=skips["no_open"],
        atr_skips=skips["no_atr"],
        cash_skips=skips["cash"] + skips["cash_short_estimated"],
        rights_skips=skips["unverified_entry_right"],
        adjustments=sum(v for k, v in hits.items() if k.startswith("holding_adjustment_")),
        writeoffs=hits["writeoff_zero_recovery"],
        fallback_marks=hits["mark_fallback_entry"],
        fallback_exits=hits["expiry_fallback_entry"],
        missing_stop_sessions=hits["stop_observation_missing"],
        odd_lot_exits=hits["odd_lot_model_sale"],
        cost_total=sum(
            e["cost"]
            for e in acct.events
            if e["action"] != "ADJUST" and "cost" in e and e["date"] <= through
        ),
        realized=last["realized"],
        unrealized=last["unrealized"],
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--caps", default="5,8,10,none")
    p.add_argument("--cash-policy", choices=("morning", "reserved", "both"), default="both")
    args = p.parse_args()
    project = args.project.resolve()
    sys.path.insert(0, str(project / "src"))
    sys.path.insert(0, str(project / "scripts/swing_gap_atr"))
    import continuous as source
    from trading_system.research import swing_forward as sf

    caps = [None if s.lower() == "none" else int(s) for s in args.caps.split(",")]
    if len(set(caps)) != len(caps) or any(x is not None and x <= 0 for x in caps):
        p.error("caps must be distinct positive integers or none")
    policies = ("morning", "reserved") if args.cash_policy == "both" else (args.cash_policy,)
    legacy = project / "data/offline-verification"
    archive = legacy / "swing-agent-replay-20261113"
    study = legacy / "swing-periods-ma-jsf-20261113/outputs"
    ranks = source.frozen_ranks(archive / "archive", study)
    nov, credits = source.november_ranks(archive)
    cutoffs, expected, proofs = source.november_extension(project, sf, nov, credits)
    assert len(nov) == 11 and not (set(nov) & set(ranks))
    all_ranks = dict(sorted({**ranks, **nov}.items()))
    codes = {c for arms in all_ranks.values() for c in arms["plus3"]}
    market = source.Market(project, study, codes, cutoffs, expected)
    first_i = market.indices[min(all_ranks)]
    last_i = market.indices[max(all_ranks)]
    assert list(all_ranks) == market.sessions[first_i : last_i + 1]
    accts = [
        Account(pol, cap, scope)
        for scope in ("history", "november")
        for pol in policies
        for cap in caps
    ]
    if None in caps:
        accts.extend(Account(pol, None, "history_zero_recovery") for pol in policies)
    frozen = []
    first_dates = {"history": min(ranks), "november": min(nov), "history_zero_recovery": min(ranks)}
    for a in accts:
        a.snapshot(first_dates[a.scope], D(0), 0, 0, 0)
    for i in range(first_i, last_i + 1):
        d, entry, due = market.sessions[i], market.sessions[i + 1], market.sessions[i + 7]
        picks = reference_list(market, d, all_ranks[d]["plus3"])
        frozen.extend(dict(signal=d, entry=entry, **r) for r in picks)
        bars = market.bars(entry)
        for a in accts:
            if d >= first_dates[a.scope]:
                run_day(a, entry, due, picks, bars, sf)
    args.output.mkdir(exist_ok=True, parents=True)

    def save(name, records):
        source.write_csv(args.output / (name + ".csv"), records)

    save("candidates", frozen)
    availability = {r["decision"]: r for r in source.rows(study / "credit-availability.csv")}
    save(
        "signals",
        [
            dict(
                decision=d,
                entry=market.sessions[market.indices[d] + 1],
                candidates=len(v["plus3"]),
                source="archive" if d in ranks else "november",
                jsf_same_day_pair=availability[d]["same_day_pair_by_cutoff"]
                if d in ranks
                else credits[d],
            )
            for d, v in all_ranks.items()
        ],
    )
    save(
        "end_positions",
        [
            dict(
                **a.ident(),
                code=c,
                entry_day=p.entered,
                due=p.due,
                quantity=p.shares,
                entry=p.entry,
                stop=p.stop,
                close=market.bars(a.nav[-1]["date"])[c]["C"],
                entry_debit=p.debit,
            )
            for a in accts
            for c, p in a.positions.items()
        ],
    )
    for field_name, filename in (
        ("events", "events"),
        ("trades", "trades"),
        ("exceptions", "exceptions"),
        ("nav", "nav"),
    ):
        save(filename, [r for a in accts for r in getattr(a, field_name)])
    summary = []
    for a in accts:
        views = {"full": a.nav[-1]["date"]}
        if a.scope.startswith("history"):
            views.update(
                common_38="2023-12-27",
                archive_last_decision="2026-10-30",
                archive_last_execution="2026-11-02",
            )
        strict_dates = [e["date"] for e in a.exceptions if e["strict_break"]]
        first_problem = min(strict_dates, default=None)
        views["strict_to_first_problem"] = (
            market.sessions[market.indices[first_problem] - 1]
            if first_problem
            else a.nav[-1]["date"]
        )
        strict_global = [
            e["date"]
            for b in accts
            if b.scope == a.scope and b.policy == a.policy
            for e in b.exceptions
            if e["strict_break"]
        ]
        if strict_global:
            views["common_strict"] = market.sessions[market.indices[min(strict_global)] - 1]
        for tag, end in views.items():
            summary.append(
                {
                    **metrics(a, end, tag),
                    "first_problem": first_problem
                    if first_problem and first_problem <= end
                    else None,
                    "full_path_first_problem": first_problem,
                }
            )
    save("summary", summary)
    sensitivity = []
    for p in policies:
        if None not in caps:
            continue
        base, alternative = [
            next(a for a in accts if a.scope == scope and a.policy == p and a.cap is None)
            for scope in ("history", "history_zero_recovery")
        ]

        def dates_events(acct, actions):
            return {
                (e["date"], e["code"], e["action"])
                for e in acct.events
                if e["date"] > "2024-01-10" and e["action"] in actions
            }

        base_buy, zero_buy = [dates_events(a, {"BUY"}) for a in (base, alternative)]
        base_sell, zero_sell = [
            dates_events(a, {"SELL_TIME", "SELL_STOP_OPEN", "SELL_STOP_INTRADAY"})
            for a in (base, alternative)
        ]
        sensitivity.append(
            dict(
                policy=p,
                modeled_nav=base.nav[-1]["nav"],
                zero_nav=alternative.nav[-1]["nav"],
                difference=alternative.nav[-1]["nav"] - base.nav[-1]["nav"],
                buys_only_primary=len(base_buy - zero_buy),
                buys_only_zero=len(zero_buy - base_buy),
                sells_only_primary=len(base_sell - zero_sell),
                sells_only_zero=len(zero_sell - base_sell),
            )
        )
    if sensitivity:
        save("zero_recovery", sensitivity)
    proof = dict(
        archive_signals=len(ranks),
        november_signals=len(nov),
        credit=credits,
        signal_count=len(all_ranks),
        first=min(all_ranks),
        last=max(all_ranks),
        last_mark=accts[0].nav[-1]["date"],
        sources=market.used_hashes,
        ranking_evidence=proofs,
        corporate_actions=[
            dict(code=c, date=d, ratio=x, url=url) for (c, d), (x, url) in KNOWN.items()
        ],
    )
    (args.output / "inputs.json").write_text(
        json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for a in accts:
        if a.scope == "history":
            strict = next((e for e in a.exceptions if e["strict_break"]), None)
            print(a.policy, a.cap, a.nav[-1]["nav"], strict)


if __name__ == "__main__":
    main()
