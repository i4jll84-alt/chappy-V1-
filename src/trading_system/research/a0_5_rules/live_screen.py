from collections import Counter
from datetime import datetime
from math import floor

import pandas as pd

from .config import A0_5
from .data_fetcher import JST, iso
from .live_portfolio import buying_power, code_of, daystr, number, quantity
from .strategy import ranked_candidates, sector_code

LIVE_COLUMNS = [
    "signal_date",
    "order_date",
    "side",
    "Code",
    "company",
    "S33",
    "shares",
    "reason",
    "reference_close",
    "avg_trading_value_20",
    "conditional_on_sales",
    "earnings_date",
    "earnings_source",
    "warning",
]


def latest_close(calendar, now=None):
    now = pd.Timestamp(now if now is not None else datetime.now(JST))
    local = (now.tz_localize(JST) if now.tzinfo is None else now.tz_convert(JST)).tz_localize(None)
    today = local.normalize()
    if calendar.empty or today < calendar[0] or today > calendar[-1]:
        raise ValueError("現在日を覆う東証立会日カレンダーがありません。同期してください。")
    completed = (
        calendar[calendar <= today]
        if local.time() >= pd.Timestamp("15:30").time()
        else calendar[calendar < today]
    )
    if completed.empty:
        raise ValueError("直近の確定立会日を確認できません。")
    date = completed[-1]
    if (
        today in calendar
        and pd.Timestamp("09:00").time() <= local.time() < pd.Timestamp("15:30").time()
    ):
        raise ValueError("当日の寄付は通過し、引けは未確定です。引け後に生成してください。")
    return date


def freshness(history, date, cal, now=None):
    latest = latest_close(cal, now)
    if date != latest:
        raise ValueError(
            f"鮮度異常：判定日 {iso(date)}、直近の確定立会日 {iso(latest)}。発注CSVを停止しました。"
        )
    data = history.loc[history.MarketUniverse]
    if data.duplicated(["Date", "Code"]).any():
        raise ValueError("銘柄日が重複しています。")
    day = data.loc[data.Date.eq(date)]
    required = [
        "PriorHigh",
        "VolumeRatio",
        "AvgTradingValue20",
        "SectorReturn",
        "SectorShare",
        "PriorShare",
        "IndexReturn",
        "C",
    ]
    complete = day.MarginKnown & day.FinKnown & day.FlowKnown & day[required].notna().all(axis=1)
    ready = int(complete.sum())
    eligible = int(day.Eligible.sum())
    known = int(day.MarginKnown.sum())
    prior = data.loc[data.Date.lt(date)].groupby("Date").MarginKnown.sum().sort_index().tail(5)
    reference = float(prior.median()) if len(prior) else None
    minimum = 0.8 * max(len(day), reference or 0)
    if ready < 2500 or known < minimum:
        raise ValueError(
            f"鮮度異常：データ完備 {ready:,}（下限2,500）、"
            f"流動性適用後 {eligible:,}、有効信用残 {known:,}"
            f"（下限 {minimum:,.1f}、対象 {len(day):,}）。発注CSVを停止しました。"
        )
    return dict(
        data_ready=ready,
        eligible=eligible,
        margin_known=known,
        margin_reference=reference,
        margin_floor=minimum,
    )


def plan(day, date, calendar, account, calendar_info, actions, config=A0_5):
    i = calendar.get_loc(date)
    if len(calendar) <= i + 3:
        raise ValueError("決算3立会日の対象末日を確認できません。カレンダーを更新してください。")
    nxt, end = map(iso, (calendar[i + 1], calendar[i + 3]))
    lookup = day.set_index("Code")
    number(account["cash"])
    for item in account["settlements"]:
        number(item["amount"], positive=True)
        daystr(item["date"])
    held = account["positions"]
    for p in held:
        code_of(p["Code"])
        quantity(p["shares"])
    if len({p["Code"] for p in held}) != len(held):
        raise ValueError("保有銘柄が重複しています。")
    held_codes = {p["Code"] for p in held}
    sells = []
    retained = Counter()
    existing = Counter()
    marked = 0.0
    alerts = []

    def append(side, c, qty, reason, sector, conditional=False):
        r = lookup.loc[c]
        return dict(
            signal_date=iso(date),
            order_date=nxt,
            side=side,
            Code=c,
            company=r.CoName,
            S33=sector,
            shares=int(qty),
            reason=reason,
            reference_close=float(r.C),
            avg_trading_value_20=r.AvgTradingValue20,
            conditional_on_sales=conditional,
            earnings_date=calendar_info[c][0],
            earnings_source=calendar_info[c][1],
            warning=actions.get(c, ""),
        )

    for p in held:
        c = p["Code"]
        if c not in lookup.index or pd.isna(lookup.at[c, "C"]) or not lookup.at[c, "Listed"]:
            raise ValueError(f"{c} の保有株評価に必要な上場情報・終値を確認してください。")
        r = lookup.loc[c]
        entry = pd.Timestamp(p["entry_date"])
        if entry > date or (entry >= calendar[0] and entry not in calendar):
            raise ValueError(f"{c} の保有開始日を確認してください。")
        age = i - calendar.searchsorted(entry) + 1
        scheduled, source = calendar_info[c]
        sector = sector_code(r.S33) or sector_code(p.get("S33"))
        if not sector:
            raise ValueError(f"{c} の保有業種を確認してください。")
        existing[sector] += 1
        marked += p["shares"] * r.C
        reason = ""
        if scheduled and iso(date) < scheduled <= nxt:
            reason = "EARNINGS_AVOID"
        elif r.C < r.ExitMA:
            reason = "SMA10_BREAK"
        elif age >= 15:
            reason = "MAX_HOLD_15D"
        elif p.get("pending_exit"):
            reason = p["pending_exit"]
        if not reason and pd.isna(r.ExitMA):
            alerts.append(f"{c}：10日線が欠損。保有株の手仕舞いを手動確認してください。")
        if scheduled is None:
            alerts.append(f"{c}：{source}（保有株の決算回避判定）")
        if p.get("warning"):
            alerts.append(f"{c}：{p['warning']}")
        if c in actions:
            alerts.append(f"{c}：{actions[c]}。株数・取得単価と10日線の連続性を確認してください。")
        if reason:
            sells.append(append("SELL", c, p["shares"], reason, sector))
        else:
            retained[sector] += 1
    priority = {"EARNINGS_AVOID": 0, "SMA10_BREAK": 1, "MAX_HOLD_15D": 2}
    sells.sort(key=lambda o: (priority[o["reason"]], o["Code"]))
    equity = account["cash"] + marked
    available = buying_power(account, date)
    if available < 0:
        raise ValueError("未受渡拘束額が現金残高を超えています。口座を確認してください。")
    free = max(0, config.slots - sum(retained.values()))
    slots = min(free, config.max_daily_entries)
    sector_free = {
        s: max(0, config.max_positions_per_sector - retained[s])
        for s in sorted(set(day.S33.dropna().astype(str)) | set(existing))
    }
    budget = available
    buys = []
    selected = Counter()
    notes = {}
    for r in ranked_candidates(day).itertuples():
        c = r.Code
        s = sector_code(r.S33)
        scheduled, source = calendar_info[c]
        near = bool(scheduled and nxt <= scheduled <= end)
        reason = ""
        if c in held_codes:
            reason = "保有中"
        elif not slots:
            reason = "保有枠・日次枠外"
        elif not s or retained[s] + selected[s] >= config.max_positions_per_sector:
            reason = "業種枠外"
        else:
            unit = r.C * (1 + config.slip_bps / 10000) * (1 + config.fee_bps / 10000)
            shares = floor(min(equity * 0.2, budget) / (unit * config.lot)) * config.lot
            if shares < config.lot:
                reason = "単元予算不足"
            else:
                # Reserve original choices before applying earnings exclusions. No replacements.
                slots -= 1
                selected[s] += 1
                budget -= shares * unit
                if not near and scheduled is not None:
                    conditional = (
                        len(held) + len(buys) + 1 > config.slots
                        or existing[s] + sum(b["S33"] == s for b in buys) + 1
                        > config.max_positions_per_sector
                    )
                    buys.append(append("BUY", c, shares, "A0_5", s, conditional))
                    reason = "寄成買い"
        if near:
            reason = "決算3営業日以内見送り"
        elif scheduled is None:
            reason = "決算予定未確認・買い保留"
        notes[c] = dict(
            decision=reason,
            earnings=scheduled,
            earnings_source=source,
            split_alert=actions.get(c, ""),
        )
    unknown = sum(r["earnings"] is None for r in notes.values())
    if unknown:
        alerts.append(
            f"買い候補のうち{unknown}銘柄の決算予定が未確認です。対象の買いは保留し、口座・決算予定で手動確認できます。"
        )
    summary = dict(
        equity=equity,
        available=available,
        locked=account["cash"] - available,
        free_slots=free,
        sector_free=sector_free,
        alerts=alerts,
        revision=account["revision"],
    )
    return pd.DataFrame(sells + buys, columns=LIVE_COLUMNS), notes, summary
