"""Plan C close-of-session indicators. Inputs must already be frozen at the cutoff."""

import math
from statistics import median

import numpy as np
import pandas as pd

REQUIRED_SESSIONS = 75
RSI_METHOD = "Wilder"
RSI_PERIOD = 2
DEFAULT_RSI_THRESHOLD = 15
MIN_COMPLETE = 2500
MIN_AVG_VALUE = 500_000_000
MIN_MARGIN_COVERAGE = 0.8


def wilder_rsi2(prices):
    """Seed with the first two close changes, then apply Wilder's 1/2 recursion."""
    changes = np.diff(prices, axis=0)
    gains, losses = np.maximum(changes, 0), np.maximum(-changes, 0)
    n = RSI_PERIOD
    if len(changes) < n:
        raise ValueError("insufficient_rsi_closes")
    up, down = gains[:n].mean(axis=0), losses[:n].mean(axis=0)
    for gain, loss in zip(gains[n:], losses[n:], strict=True):
        up, down = ((n - 1) * up + gain) / n, ((n - 1) * down + loss) / n
    total = up + down
    return np.divide(100 * up, total, out=np.full_like(total, 50.0), where=total != 0)


def _dated(frame):
    frame = frame.copy()
    frame["Date"] = pd.to_datetime(frame["Date"])
    if "Code" in frame:
        frame["Code"] = frame["Code"].astype(str)
    return frame


def _universe(master, date):
    rows = master.loc[master.Date.eq(date)]
    return rows.loc[
        rows.Mkt.isin(("0111", "0112")) & rows.ProdCat.eq("011") & rows.Code.str.endswith("0")
    ].set_index("Code", verify_integrity=True)


def _balances(margin, date, bars, sessions):
    issued = margin.loc[margin.PubDate.le(date) & margin.Date.lt(margin.PubDate)]
    if "AvailableDay" in issued:
        issued = issued.loc[issued.AvailableDay.le(date)]
    # Plan B's guard requires the market's latest reporting period rather than
    # each code's last non-null balance. An absent publication date is unknown.
    end = issued.Date.max()
    before = sessions[sessions < date][-20:]
    if pd.isna(end) or not 1 <= (date - end).days <= 10 or len(before) != 20:
        return pd.Series(dtype=float)
    order = ["PubDate", "AvailableDay"] if "AvailableDay" in issued else ["PubDate"]
    rows = (
        issued.loc[issued.Date.eq(end)]
        .sort_values(order, kind="stable")
        .drop_duplicates("Code", keep="last")
    )
    values = pd.to_numeric(rows.set_index("Code").LongVol, errors="coerce")
    history = bars.loc[bars.Date.isin(before), ["Code", "Vo"]]
    vol = pd.to_numeric(history.Vo, errors="coerce")
    known = vol.where(np.isfinite(vol) & vol.ge(0)).groupby(history.Code).agg(["count", "sum"])
    known = known.index[(known["count"] == 20) & (known["sum"] > 0)]
    after = bars.loc[bars.Date.gt(end) & bars.Date.le(date)]
    actions = after.loc[
        pd.to_numeric(after.AdjFactor, errors="coerce").ne(1)
        | ~after.ExRT.fillna("").isin(["", "0", 0]),
        "Code",
    ]
    return (
        values.where(np.isfinite(values) & values.ge(0))
        .dropna()
        .reindex(known.difference(actions.unique()))
        .dropna()
    )


def evaluate(
    sessions,
    bars,
    master,
    topix,
    margin,
    earnings,
    *,
    rsi_threshold=DEFAULT_RSI_THRESHOLD,
    enable_vol_circuit_breaker=False,
    vol_threshold=30,
    vol_value=None,
):
    """Evaluate the final session. Later rows are ignored. No liquidity filter in readiness."""
    if not math.isfinite(rsi_threshold) or not 0 <= rsi_threshold <= 100:
        raise ValueError("invalid_rsi_threshold")
    if not math.isfinite(vol_threshold) or vol_threshold <= 0:
        raise ValueError("invalid_vol_threshold")
    sessions = pd.DatetimeIndex(sessions)
    if sessions.empty or not sessions.is_monotonic_increasing or not sessions.is_unique:
        raise ValueError("invalid_calendar")
    days = sessions[-REQUIRED_SESSIONS:]
    day = days[-1]
    bars, master, topix, margin = map(_dated, (bars, master, topix, margin))
    margin["PubDate"] = pd.to_datetime(margin.PubDate)
    if "AvailableDay" in margin:
        margin["AvailableDay"] = pd.to_datetime(margin.AvailableDay)
    bars = bars.loc[bars.Date.isin(days)]
    codes = pd.Index(sorted(bars.Code.unique()), name="Code")
    today = _universe(master, day)
    top = topix.loc[topix.Date.isin(days)].set_index("Date", verify_integrity=True)
    benchmark = pd.to_numeric(top.C.reindex(days), errors="coerce").to_numpy(dtype=float)
    balances = _balances(margin, day, bars, days)
    known = len(today.index.intersection(balances.index))
    prior = []
    for earlier in days[-6:-1]:
        universe = _universe(master, earlier)
        if not universe.empty:
            prior.append(
                len(universe.index.intersection(_balances(margin, earlier, bars, days).index))
            )
    reference = median(prior) if prior else None
    minimum = MIN_MARGIN_COVERAGE * max(len(today), reference or 0)
    counts = dict(
        market_universe=len(today),
        margin_known=known,
        margin_reference=reference,
        margin_floor=minimum,
        data_ready=0,
        eligible=0,
    )
    reasons, marks, ranked = [], {}, []
    if len(days) < REQUIRED_SESSIONS:
        reasons.append("insufficient_price_history")
    if today.empty:
        reasons.append("master_unavailable")
    if known < minimum:
        reasons.append("margin_coverage_below_floor")
    market_known = (
        len(days) == REQUIRED_SESSIONS and np.isfinite(benchmark).all() and (benchmark > 0).all()
    )
    if not market_known:
        reasons.append("topix_history_unavailable")
    if enable_vol_circuit_breaker and (
        vol_value is None or not math.isfinite(vol_value) or vol_value < 0
    ):
        reasons.append("volatility_unavailable")

    def matrix(field):
        subset = bars[["Date", "Code", field]].copy()
        subset[field] = pd.to_numeric(subset[field], errors="coerce")
        a = subset.pivot(index="Date", columns="Code", values=field)
        arr = a.reindex(index=days, columns=codes).to_numpy(dtype=float, copy=True)
        arr[~np.isfinite(arr)] = np.nan
        return arr

    closes, factors = matrix("C"), matrix("AdjFactor")
    rights = bars.pivot(index="Date", columns="Code", values="ExRT").reindex(
        index=days, columns=codes
    )
    comparable = (factors == 1) & rights.fillna("").isin(("", "0", 0)).to_numpy(dtype=bool)
    closes[closes <= 0] = np.nan
    last = closes[-1]
    if len(days) >= 5:
        sma5 = np.mean(closes[-5:], axis=0)
        sma5[~comparable[-5:].all(axis=0)] = np.nan
    else:
        sma5 = np.full(len(codes), np.nan)
    for idx in np.flatnonzero(np.isfinite(last)):
        marks[codes[idx]] = dict(
            close=float(last[idx]), sma5=float(sma5[idx]) if np.isfinite(sma5[idx]) else None
        )
    if len(days) == REQUIRED_SESSIONS and len(codes):
        high, low, value, volume = (matrix(k) for k in ("H", "L", "Va", "Vo"))
        sma25, sma75 = closes[-25:].mean(axis=0), closes.mean(axis=0)
        rsi = wilder_rsi2(closes)
        tr = np.maximum(high[-14:], closes[-15:-1]) - np.minimum(low[-14:], closes[-15:-1])
        atr = tr.mean(axis=0)
        # The credit-balance denominator uses the 20 completed sessions before the signal day.
        average_volume = volume[-21:-1].mean(axis=0)
        average_value = value[-20:].mean(axis=0)
        available = balances.reindex(codes).to_numpy(dtype=float)
        sector = today.S33.reindex(codes)
        known_sector = sector.notna().to_numpy() & ~sector.fillna("").isin(["", "9999"]).to_numpy()
        known_earnings = np.array([isinstance(earnings.get(c), (list, tuple)) for c in codes])
        valid_atr = (
            np.isfinite(atr)
            & (atr > 0)
            & (high[-14:] >= closes[-14:]).all(axis=0)
            & (low[-14:] <= closes[-14:]).all(axis=0)
            & (low[-14:] > 0).all(axis=0)
        )
        signal_inputs = (
            codes.isin(today.index)
            & np.isfinite(sma75)
            & comparable.all(axis=0)
            & np.isfinite(rsi)
            & np.isfinite(average_volume)
            & (volume[-21:-1] >= 0).all(axis=0)
            & (average_volume > 0)
            & np.isfinite(average_value)
            & (value[-20:] >= 0).all(axis=0)
            & np.isfinite(available)
        )
        complete = signal_inputs & known_earnings & valid_atr & known_sector
        liquid = signal_inputs & (average_value >= MIN_AVG_VALUE)
        counts.update(data_ready=int(complete.sum()), eligible=int(liquid.sum()))
        choose = (
            liquid
            & (last > sma75)
            & (sma25 > sma75)
            & (last < sma5)
            & (rsi <= rsi_threshold + 1e-12)
            & (available <= 5 * average_volume)
        )
        for idx in np.flatnonzero(choose):
            ranked.append(
                dict(
                    code=str(codes[idx]),
                    sector=str(sector.iloc[idx]) if known_sector[idx] else None,
                    close=float(last[idx]),
                    atr14=float(atr[idx]) if valid_atr[idx] else None,
                    deviation=float(last[idx] / sma25[idx] - 1),
                    rsi2=float(rsi[idx]),
                )
            )
    if counts["data_ready"] < MIN_COMPLETE:
        reasons.append("data_ready_below_2500")
    coverage_ok = not reasons
    if market_known and benchmark[-1] <= benchmark.mean():
        reasons.append("topix_not_above_sma75")
    if enable_vol_circuit_breaker and vol_value is not None and vol_value >= vol_threshold:
        reasons.append("volatility_at_or_above_threshold")
    return dict(
        ranked=sorted(ranked, key=lambda r: (r["deviation"], r["code"])) if not reasons else [],
        marks=marks,
        earnings=earnings,
        counts=counts,
        coverage_ok=coverage_ok,
        allow_entries=not reasons,
        reasons=reasons,
        indicators=dict(rsi_method=RSI_METHOD, rsi_period=RSI_PERIOD, rsi_threshold=rsi_threshold),
    )
