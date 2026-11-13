"""Independent short-sale research signals and public eligibility checks."""

from decimal import Decimal as D

from trading_system.research.swing_short_sources import PublicInputs, margin_rows

EQUITY_INDICES = ("SP500", "NASDAQCOM", "NK225F")
ARMS = ("chart", "credit", "market")


def ratio(numerator, denominator):
    if numerator in (None, "") or denominator in (None, ""):
        return None
    a, b = D(str(numerator).replace(",", "")), D(str(denominator).replace(",", ""))
    if not a.is_finite() or not b.is_finite() or a < 0 or b <= 0:
        return None
    return str(a / b)


def chart_rows(history, master, window):
    result = []
    for code, info in master.items():
        if (
            info.get("MrgnNm") != "貸借"
            or info["ProdCat"] != "011"
            or not code.endswith("0")
            or info["Mkt"] not in ("0111", "0112", "0113")
        ):
            continue
        bars = [history[d].get(code) for d in window]
        if any(not b or not b["C"] or D(str(b["C"])) <= 0 for b in bars):
            continue
        if any(
            str(b["AdjFactor"]) not in ("1", "1.0") or b["ExRT"] not in (None, "") for b in bars[1:]
        ):
            continue
        closes = [D(str(b["C"])) for b in bars]
        close = closes[-1]
        prior5 = sum(closes[-6:-1]) / 5
        ma5, ma20 = sum(closes[-5:]) / 5, sum(closes[-20:]) / 20
        va = D(str(bars[-1]["Va"] or 0))
        if not (
            close < closes[0]
            and close < ma5 < ma20
            and ma5 < prior5
            and va >= 100000000
            and close * 100 * D("1.0005") + 10 <= 200000
        ):
            continue
        va5 = [D(str(b["Va"] or 0)) for b in bars[-6:-1]]
        turnover = ratio(va, sum(va5) / 5) if min(va5) > 0 else None
        result.append(
            dict(
                code=code,
                close=str(close),
                return20=str(close / closes[0] - 1),
                ma5=str(ma5),
                ma20=str(ma20),
                previous_ma5=str(prior5),
                below_previous_5_closes=close < min(closes[-6:-1]),
                turnover_multiple=turnover,
            )
        )
    return sorted(result, key=lambda r: (D(r["return20"]), r["code"]))


def market_check(market):
    series = market.get("series", {})
    if any(series.get(k, {}).get("state") != "usable" for k in EQUITY_INDICES):
        return "market_data_missing"
    return (
        "pass"
        if sum(D(series[k]["change"]) < 0 for k in EQUITY_INDICES) >= 2
        else "market_not_bearish"
    )


def credit_details(row, loans, days, exchange, publications):
    code = row["code"]
    records = [loans.get(d, {}).get(code, {}) for d in days]
    current, prior = records
    ratios = [ratio(r.get("融資残高株数"), r.get("貸株残高株数")) for r in records]
    ex = [r.get(code, {}) for r in exchange]
    ex_ratios = [ratio(r.get("LongVol"), r.get("ShrtVol")) for r in ex]
    loan_shares = [r.get("貸株残高株数") for r in records]
    delta = (
        D(loan_shares[0]) - D(loan_shares[1])
        if all(v not in (None, "") for v in loan_shares)
        else None
    )
    flags = []
    if row["turnover_multiple"] is None or D(row["turnover_multiple"]) < 1:
        flags.append("turnover_not_confirmed")
    if None in ratios or delta is None:
        flags.append("loan_ratio_or_change_missing")
    else:
        if D(ratios[0]) < 1:
            flags.append("loan_ratio_below_one")
        if delta > 0:
            flags.append("stock_borrow_increased")
    if ex_ratios[0] is None:
        flags.append("exchange_margin_ratio_missing")
    elif D(ex_ratios[0]) < 1:
        flags.append("exchange_margin_ratio_below_one")
    return dict(
        loan_dates=days,
        loan_ratios=ratios,
        borrow_shares=loan_shares,
        financing_shares=[current.get("融資残高株数"), prior.get("融資残高株数")],
        exchange_publications=publications,
        exchange_dates=[r.get("Date") for r in ex],
        exchange_volume=[(r.get("LongVol"), r.get("ShrtVol")) for r in ex],
        exchange_ratios=ex_ratios,
        exclusions=flags,
    )


def prepare(project, entry, window, charts, market, cutoff, reconstructed=False):
    prior = window[-1]
    sources = PublicInputs(project, cutoff)
    try:
        allowed, state = sources.restrictions(prior, entry)
        pool = [r for r in charts if allowed is not None and r["code"] in allowed][:5]
        loans = sources.loan_history(window[-5:]) if pool else {}
    finally:
        sources.db.close()
    latest = max(loans, default=None)
    dates = [None, None]
    if latest in window[-3:]:
        dates = [latest, window[window.index(latest) - 1]]
    dates_ex = window[-2:][::-1]
    reports = [margin_rows(project, d, None if reconstructed else cutoff) for d in dates_ex]
    exchange = [r[0] for r in reports]
    market_state = market_check(market)
    annotated = []
    for row in pool:
        credit = credit_details(row, loans, dates, exchange, dates_ex)
        annotated.append(dict(**row, credit=credit))
    return dict(
        state=state,
        cutoff=cutoff.isoformat(),
        target=entry,
        signal_date=prior,
        blocked_count=len(charts) - sum(r["code"] in allowed for r in charts)
        if allowed is not None
        else None,
        market_state=market_state,
        candidates=annotated,
        public_sources=sources.refs,
        exchange_sources=[r[1] for r in reports],
        exchange_reconstructed=reconstructed,
        broker_acceptance="unverified",
    )


def choices(packet, arm):
    rows = packet["candidates"]
    if arm == "chart":
        return [r["code"] for r in rows]
    if arm not in ("credit", "market"):
        raise ValueError("unknown_short_arm")
    if arm == "market" and packet["market_state"] != "pass":
        return []
    return [r["code"] for r in rows if not r["credit"]["exclusions"]]


def review(pre, answer):
    packet = pre.get("short_research", {})
    if (
        not isinstance(answer, dict)
        or not isinstance(answer.get("reason"), str)
        or not answer["reason"].strip()
    ):
        return {"state": "assessment_missing", "watch": []}
    codes = answer.get("watch")
    allowed = (
        set(choices(packet, "market"))
        if packet.get("state") == "available" and not pre["stopped"]
        else set()
    )
    if not isinstance(codes, dict) or any(
        c not in allowed or not isinstance(v, str) or not v.strip() for c, v in codes.items()
    ):
        return {"state": "invalid_short_watch", "watch": []}
    return {"state": "recorded", "watch": list(codes), "broker_acceptance": "unverified"}
