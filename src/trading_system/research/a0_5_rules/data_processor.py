"""All dates are exchange-local dates. A release must precede the signal date."""

import json
from itertools import groupby
from operator import attrgetter

import numpy as np
import pandas as pd

from .config import (
    ATR_PERIOD,
    FINANCIAL_HISTORY_DAYS,
    MARGIN_BALANCE_FALLBACK_OFFSET_BDAYS,
    MIN_AVG_TRADING_VALUE_20,
)
from .data_fetcher import read_parts
from .strategy import market_entry_signal, trend_signal

MARKET_REFORM = pd.Timestamp("2022-04-04")
FIRST_PRIME_PUBLICATION = pd.Timestamp("2022-04-14")
PRE_REFORM_MARKETS = ("0101", "0102", "0104", "0106", "0107")
POST_REFORM_MARKETS = ("0111", "0112")


def unique(df, keys, name):
    if df.duplicated(keys).any():
        raise ValueError(f"{name} に同一キーの異なるレコードがあります。再同期・確認が必要です。")
    return df


def lag_join(left, right, date_col, by=None):
    """Join the latest known publication strictly before each TSE session date.

    Date and date_col are exchange-local dates without time-of-day. The left
    axis supplies the exchange calendar, so the first match after a release
    is the next session even across weekends or exchange holidays.
    """
    right = right.dropna(subset=[date_col])
    if right.empty:
        result = left.copy()
        for col in right.columns.difference(left.columns):
            result[col] = pd.NaT if col.endswith(("Date", "End")) else np.nan
        return result
    return pd.merge_asof(
        left.sort_values("Date"),
        right.sort_values(date_col),
        left_on="Date",
        right_on=date_col,
        by=by,
        allow_exact_matches=False,
        direction="backward",
    )


def financial_events(df):
    cols = ["Code", "FinDate", "FinEnd", "Sales", "PriorSales", "OP", "PriorOP"]
    df = (
        df.loc[df.DocType.str.contains("FinancialStatements", na=False)]
        .dropna(subset=["CurPerSt", "CurPerEn", "CurPerType", "DiscDate"])
        .copy()
    )
    if df.empty:
        return pd.DataFrame(columns=cols)
    # Match the accounting basis and cumulative reporting interval (including leap-year ends).
    for col in ("CurPerSt", "CurPerEn"):
        shifted = df[col] - pd.DateOffset(years=1)
        df["prior" + col] = shifted.where(
            ~df[col].dt.is_month_end, shifted + pd.offsets.MonthEnd(0)
        )
    df = df.sort_values(["Code", "DiscDate", "DiscTime"], na_position="first")
    rows = []
    for code, issues in df.groupby("Code", sort=False):
        known = {}
        latest_end = pd.Timestamp.min
        latest_key = None
        for pub, filings in groupby(issues.itertuples(index=False), attrgetter("DiscDate")):
            for filing in filings:
                key = (filing.CurPerEn, filing.CurPerSt, filing.DocType)
                prior = (filing.priorCurPerEn, filing.priorCurPerSt, filing.DocType)
                value = (filing.Sales, filing.OP, prior, pub, filing.DiscTime)
                old = known.get(key)
                if old and old[3:] == value[3:] and old[:2] != value[:2]:
                    value = (np.nan, np.nan, prior, pub, filing.DiscTime)
                known[key] = value
                if filing.CurPerEn >= latest_end:
                    latest_end, latest_key = filing.CurPerEn, key
            sales, op, prior, _, _ = known[latest_key]
            previous = known.get(prior, (np.nan, np.nan))
            rows.append((code, pub, latest_end, sales, previous[0], op, previous[1]))
    return pd.DataFrame(rows, columns=cols).astype({"Code": "string"})


def latest_reporting_period(df, groups, period, publication):
    """A late correction to an older period cannot displace a newer observation."""
    frame = df.dropna(subset=[period, publication]).sort_values([publication, period])
    if frame.empty:
        return frame
    latest = (
        frame.groupby(groups, sort=False)[period].cummax() if groups else frame[period].cummax()
    )
    keys = [*groups, publication]
    return frame.loc[frame[period].eq(latest)].drop_duplicates(keys, keep="last")


def rolling(frame, value, window, operation, shift=0):
    grouped = frame.groupby("Code", sort=False)[value]
    values = grouped.shift(shift) if shift else frame[value]
    series = getattr(values.groupby(frame.Code, sort=False).rolling(window), operation)()
    return series.droplevel(0).reindex(frame.index)


def margin_events(raw, sessions):
    margin = (
        unique(raw, ["Code", "Date", "PubDate"], "信用残")
        .rename(columns={"Date": "MarginEnd", "PubDate": "MarginPubDate"})
        .copy()
    )
    # Never turn a present but inconsistent date into a synthetic publication.
    missing = margin.MarginPubDate.isna()
    margin["is_published_date_estimated"] = missing.astype("boolean")
    margin["MarginDate"] = margin.MarginPubDate.where(margin.MarginPubDate > margin.MarginEnd)
    offset_dates = pd.Series(sessions, index=sessions).shift(-MARGIN_BALANCE_FALLBACK_OFFSET_BDAYS)
    estimates = margin.loc[missing, "MarginEnd"].map(offset_dates)
    if margin.duplicated(["Code", "MarginEnd"]).any():
        # An already public version wins. A later revision cannot change earlier inputs.
        earliest = margin.groupby(["Code", "MarginEnd"])["MarginDate"].transform("min").loc[missing]
        estimates = estimates.where(earliest.isna() | estimates.lt(earliest))
    margin.loc[missing, "MarginDate"] = estimates
    return latest_reporting_period(margin, ["Code"], "MarginEnd", "MarginDate")


def current_weekly_reference(sessions):
    """Most recent scheduled weekly balance usable on each session (exchange calendar only)."""
    dates = pd.Series(sessions, index=sessions)
    weeks = dates.groupby(sessions.to_period("W-SUN"))
    # The old J-Quants series omits weeks with two sessions or fewer.
    week_ends = weeks.max()[weeks.size() > 2]
    return (
        dates.where(dates.isin(week_ends)).shift(MARGIN_BALANCE_FALLBACK_OFFSET_BDAYS + 1).ffill()
    )


def trend_history(path, calendar, start, end):
    # Only prices need the extra history. Extending other inputs could change A0.
    first = max(0, calendar.searchsorted(start) - 199)
    sessions = calendar[first : calendar.searchsorted(end, side="right")]
    bars = read_parts(path, "bars", sessions[0], end, columns=["Date", "Code", "C", "AdjFactor"])
    return bars, sessions


def add_trend_averages(frame, bars, sessions):
    """Require 50/200 consecutive exchange closes, including the signal close."""
    bars = unique(bars, ["Date", "Code"], "長期移動平均用日足")
    prices = bars.pivot(index="Date", columns="Code", values=["C", "AdjFactor"]).reindex(sessions)
    close, factors = prices["C"], prices["AdjFactor"]
    if (factors <= 0).any().any() or (close.notna() & factors.isna()).any().any():
        raise ValueError("長期移動平均の株価調整係数が不正です。")
    scale = factors.fillna(1).cumprod()
    adjusted = close / scale
    days = sessions.get_indexer(frame.Date)
    codes = close.columns.get_indexer(frame.Code)
    available = (days >= 0) & (codes >= 0)
    result = frame.copy(deep=False)
    for length in (50, 200):
        mean = adjusted.rolling(length, min_periods=length).mean() * scale
        values = np.full(len(frame), np.nan)
        values[available] = mean.to_numpy()[days[available], codes[available]]
        result[f"SMA{length}"] = values
    return result


def topix_sma75(prices, sessions):
    close = unique(prices, ["Date"], "TOPIX").set_index("Date").C.reindex(sessions)
    return close.rolling(75, min_periods=75).mean().rename("TOPIXSMA75")


def snapshot_topix_sma75(path, calendar, start, end):
    first = max(0, calendar.searchsorted(start) - 74)
    dates = calendar[first : calendar.searchsorted(end, side="right")]
    return topix_sma75(read_parts(path, "topix", dates[0], dates[-1]), dates)


def features(raw, sessions, cfg, *, price_history=None, market_average=None):
    bars = unique(raw["bars"], ["Date", "Code"], "日足")
    master = unique(raw["master"], ["Date", "Code"], "銘柄マスター")
    if "Mkt" not in master:
        raise ValueError(
            "銘柄マスターに市場区分 Mkt がありません。"
            "市場区分付きスナップショットを選んでください。"
        )
    if "S33" not in master and cfg.max_positions_per_sector:
        raise ValueError(
            "旧版の銘柄マスターに33業種コードがありません。同じ期間を差分取得し、新しいスナップショットを選んでください。"
        )
    if "S33" not in master:
        master = master.assign(S33=pd.NA)
    topix = unique(raw["topix"], ["Date"], "TOPIX").sort_values("Date").copy()
    sessions = pd.DatetimeIndex(sessions).astype("datetime64[ns]")
    topix = topix.set_index("Date").reindex(sessions)
    if cfg.require_topix_above_sma75:
        topix["TOPIXSMA75"] = (
            topix_sma75(raw["topix"], sessions) if market_average is None else market_average
        )
    topix["MarketOK"] = topix.C > topix.C.rolling(cfg.window).mean()
    topix["IndexReturn"] = topix.C.pct_change(cfg.window, fill_method=None)
    topix.rename(columns={"C": "TOPIX"}, inplace=True)
    day = bars.merge(
        master, on=["Date", "Code"], how="outer", validate="one_to_one", indicator=True
    )
    day["Listed"] = day["_merge"].ne("left_only")
    day["Domestic"] = day.ProdCat.eq("011").fillna(False) & day.Code.str.endswith("0")
    before_reform = day.Date.lt(MARKET_REFORM)
    old_markets = ("0101", "0102", "0106") if cfg.exclude_pre_reform_growth else PRE_REFORM_MARKETS
    day["MarketUniverse"] = day.Domestic & (
        (before_reform & day.Mkt.isin(old_markets))
        | (~before_reform & day.Mkt.isin(POST_REFORM_MARKETS))
    )
    day = day.loc[day.Date.isin(sessions)].sort_values(["Code", "Date"]).reset_index(drop=True)
    n = cfg.window
    day["Session"] = sessions.get_indexer(day.Date)
    if (day.AdjFactor.dropna() <= 0).any() or day.loc[day.C.notna(), "AdjFactor"].isna().any():
        raise ValueError("株価調整係数が不正です。")
    factors = day.AdjFactor.fillna(1)
    price_scale = factors.groupby(day.Code).cumprod()
    vol_scale = factors.where(~day.ExRT.eq("3").fillna(False), 1).groupby(day.Code).cumprod()
    day["NormHigh"] = day.H / price_scale
    day["NormClose"] = day.C / price_scale
    day["NormVol"] = day.Vo * vol_scale
    day["PriorHigh"] = rolling(day, "NormHigh", n, "max", 1) * price_scale
    day["AvgVol"] = rolling(day, "NormVol", n, "mean", 1) / vol_scale
    day["ExitMA"] = rolling(day, "NormClose", cfg.exit_window, "mean") * price_scale
    prev = day.groupby("Code", sort=False)
    if cfg.use_atr_risk_management:
        # Common split-adjusted units across days; restore signal-day yen per share for orders.
        prev_close = prev.NormClose.shift()
        low = day.L / price_scale
        day["TrueRange"] = np.maximum(day.NormHigh, prev_close) - np.minimum(low, prev_close)
        continuous = day.Session.sub(prev.Session.shift(ATR_PERIOD)).eq(ATR_PERIOD)
        day[f"ATR_{ATR_PERIOD}"] = (
            rolling(day, "TrueRange", ATR_PERIOD, "mean") * price_scale
        ).where(continuous)
        del day["TrueRange"]
    # The closing signal can use today's turnover. All 20 exchange sessions must be present.
    day["AvgTradingValue20"] = rolling(day, "Va", 20, "mean").where(
        day.Session.sub(prev.Session.shift(19)).eq(19)
    )
    day["Return"] = day.NormClose / prev.NormClose.shift(n) - 1
    consecutive = day.Session.sub(prev.Session.shift(n)).eq(n)
    day.loc[~consecutive, ["PriorHigh", "AvgVol", "Return"]] = np.nan
    day["VolumeRatio"] = day.Vo / day.AvgVol.where(day.AvgVol > 0)
    day["LastAction"] = day.Date.where(factors.ne(1)).groupby(day.Code).ffill()

    universe = day.loc[day.MarketUniverse & day.S17.notna() & day.S17.ne("99")]
    sector = universe.groupby(["Date", "S17"], as_index=False).agg(
        SectorReturn=("Return", "median"), SectorValue=("Va", "sum")
    )
    sector["SectorShare"] = sector.SectorValue / sector.groupby("Date").SectorValue.transform("sum")
    share = sector.pivot(index="Date", columns="S17", values="SectorShare").reindex(sessions)
    prior_share = (
        share.shift()
        .rolling(n)
        .mean()
        .rename_axis(index="Date")
        .reset_index()
        .melt(id_vars="Date", value_name="PriorShare")
    )
    sector = sector.merge(prior_share, on=["Date", "S17"])
    day = day.merge(sector.drop(columns="SectorValue"), on=["Date", "S17"], how="left")
    day = day.merge(topix, left_on="Date", right_index=True, how="left")

    funds = raw["investors"]
    old_section = (
        funds.Section.eq("TSE1st")
        & funds.PubDate.lt(FIRST_PRIME_PUBLICATION)
        & funds.EnDate.lt(MARKET_REFORM)
    )
    new_section = (
        funds.Section.eq("TSEPrime")
        & funds.PubDate.ge(FIRST_PRIME_PUBLICATION)
        & funds.EnDate.ge(MARKET_REFORM)
    )
    funds = unique(
        funds.loc[(old_section | new_section) & funds.EnDate.le(funds.PubDate)],
        ["PubDate", "EnDate"],
        "投資部門別",
    )
    funds = latest_reporting_period(funds, [], "EnDate", "PubDate").rename(
        columns={"PubDate": "FlowDate", "EnDate": "FlowEnd"}
    )
    day = lag_join(day, funds[["FlowDate", "FlowEnd", "FrgnBal"]], "FlowDate")
    margin = margin_events(raw["margin"], sessions)
    releases = latest_reporting_period(margin, [], "MarginEnd", "MarginDate").rename(
        columns={"MarginDate": "MarketMarginDate", "MarginEnd": "MarketMarginEnd"}
    )
    day = lag_join(day, releases[["MarketMarginDate", "MarketMarginEnd"]], "MarketMarginDate")
    day = lag_join(
        day,
        margin[
            [
                "Code",
                "MarginDate",
                "MarginPubDate",
                "MarginEnd",
                "LongVol",
                "is_published_date_estimated",
            ]
        ],
        "MarginDate",
        "Code",
    )
    day["is_published_date_estimated"] = day.is_published_date_estimated.astype("boolean")
    day = lag_join(day, financial_events(raw["financial"]), "FinDate", "Code")
    day["MarginDays"] = day.LongVol / day.AvgVol.where(day.AvgVol > 0)
    fresh = (day.Date - day.MarginEnd).dt.days.between(1, 10)
    weekly = day.is_published_date_estimated.fillna(False) & day.MarginEnd.lt("2026-09-25")
    fresh = fresh.where(~weekly, day.MarginEnd.eq(day.Date.map(current_weekly_reference(sessions))))
    day["MarginKnown"] = (
        day.MarginDays.notna()
        & (day.LongVol >= 0)
        & day.MarginEnd.eq(day.MarketMarginEnd)
        & fresh
        & (day.LastAction.isna() | (day.LastAction <= day.MarginEnd))
    )
    day["FinKnown"] = day[["Sales", "PriorSales", "OP", "PriorOP"]].notna().all(axis=1) & (
        day.Date - day.FinEnd
    ).dt.days.between(1, 190)
    day["FlowKnown"] = day.FrgnBal.notna() & (day.Date - day.FlowEnd).dt.days.between(1, 21)
    day["Eligible"] = (
        day.MarketUniverse
        & day.AvgTradingValue20.ge(MIN_AVG_TRADING_VALUE_20)
        & day.MarginKnown
        & day.FinKnown
        & day.FlowKnown
        & day[
            [
                "PriorHigh",
                "VolumeRatio",
                "AvgTradingValue20",
                "SectorReturn",
                "SectorShare",
                "PriorShare",
                "IndexReturn",
                "C",
            ]
        ]
        .notna()
        .all(axis=1)
    )
    day["Signal"] = (
        day.Eligible
        & day.MarketOK
        & (day.FrgnBal > 0)
        & (day.SectorReturn > day.IndexReturn)
        & (day.SectorShare > day.PriorShare)
        & (day.Sales > day.PriorSales)
        & (day.OP > day.PriorOP)
        & (day.OP > 0)
        & (day.MarginDays <= cfg.margin_days)
        & (day.C > day.PriorHigh)
        & (day.VolumeRatio >= cfg.volume_multiple)
    )
    day["Signal"] = market_entry_signal(day, cfg)
    if cfg.long_trend_filter != "NONE":
        prices, trend_sessions = (bars, sessions) if price_history is None else price_history
        day = add_trend_averages(day, prices, trend_sessions)
        day["Signal"] = trend_signal(day, cfg.long_trend_filter)
    return day.drop(columns=["NormHigh", "NormClose", "NormVol", "_merge", "LastAction"])


def load_window(path, start, end, cfg):
    info = json.loads(path.read_text(encoding="utf-8"))
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start < pd.Timestamp(info["start"]) or end > pd.Timestamp(info["end"]) or start > end:
        raise ValueError("指定期間がスナップショットの検証範囲外です。")
    calendar = pd.DatetimeIndex(info["sessions"]).astype("datetime64[ns]")
    lookback = max(
        cfg.window, cfg.exit_window, 21, ATR_PERIOD if cfg.use_atr_risk_management else 0
    )
    first = max(0, calendar.searchsorted(start) - lookback - 2)
    sessions = calendar[(calendar >= calendar[first]) & (calendar <= end)]
    raw = {
        name: read_parts(
            path,
            name,
            start - pd.DateOffset(days=FINANCIAL_HISTORY_DAYS)
            if name == "financial"
            else sessions[0],
            end,
        )
        for name in info["files"]
    }
    for name in ("bars", "master", "topix"):
        found = raw[name].dropna(subset=["C"]) if name != "master" else raw[name]
        missing = sessions.difference(found.Date)
        if len(missing):
            raise ValueError(f"{name} の立会日データが欠けています {missing[0]:%Y-%m-%d}。")
    history = trend_history(path, calendar, start, end) if cfg.long_trend_filter != "NONE" else None
    average = (
        snapshot_topix_sma75(path, calendar, start, end) if cfg.require_topix_above_sma75 else None
    )
    table = features(raw, sessions, cfg, price_history=history, market_average=average)
    return table.loc[table.Date.between(start, end)].copy(), calendar
