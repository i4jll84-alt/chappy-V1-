"""Plan C (500) paper entries. Exit instructions, fills and cash accounting use the V21 engine."""

import math
import sqlite3
from collections import Counter

import duckdb
import numpy as np
import pandas as pd

from . import a0_5_inputs as saved
from . import plan_b_v21
from .swing_forward import read

VERSION = "c500-topix25-sector3-v1"
# JPX 33-sector codes; 9999 and unclassified instruments cannot consume a sector slot.
SECTORS = frozenset(
    "0050 1050 2050 3050 3100 3150 3200 3250 3300 3350 3400 3450 3500 3550 3600 3650 "
    "3700 3750 3800 4050 5050 5100 5150 5200 5250 6050 6100 7050 7100 7150 7200 8050 9050".split()
)


def market_gate(project, window, now):
    days = window[-25:]
    if len(days) != 25 or days != sorted(set(days)):
        raise ValueError("c500_topix_window_incomplete")
    root = project / "data/jquants"
    refs, closes, historical = [], {}, []
    for day in days:
        path = root / "daily-snapshots" / day / "last_collection.json"
        if not path.exists():
            historical.append(day)
            continue
        receipt = read(path)
        if receipt.get("date") != day:
            raise ValueError("c500_topix_receipt_date")
        rows = saved.market_pages(root, receipt, "topix", "Date", now, refs)
        if len(rows) != 1:
            raise ValueError("c500_topix_duplicate_or_missing")
        closes[day] = rows[0]["C"]
    if historical:
        parts = saved.bulk(
            root / "history",
            "/indices/bars/daily/topix",
            ["Date", "C"],
            historical[0],
            historical[-1],
            now,
            refs,
        )
        for frame in parts:
            if frame.Date.duplicated().any():
                raise ValueError("c500_topix_duplicate_in_source")
            for row in frame.itertuples(index=False):
                if row.Date in historical:
                    closes[row.Date] = row.C
    if set(closes) != set(days):
        raise ValueError("c500_topix_closes_missing")
    values = [float(closes[d]) for d in days]
    if not all(math.isfinite(v) and v > 0 for v in values):
        raise ValueError("c500_topix_close_invalid")
    mean = math.fsum(values) / 25
    return dict(
        date=days[-1], close=values[-1], sma25=mean, bullish=values[-1] > mean, sources=refs
    )


def order_size(close, budget):
    limit = math.floor(close * 1.015)
    unit = limit * 100
    shares = 100 * int(budget // (unit + (10.0 + unit * 0.0005)))
    amount = limit * shares
    return limit, shares, amount + (10.0 + amount * 0.0005)


def rank_candidates(frame, master, runner, budget, excluded):
    balances = ["jsf_loan", "jsf_stock", "prev_jsf_loan", "prev_jsf_stock"]
    fields = [
        "close",
        "ma5",
        "ma20",
        "ret_20d",
        "turnover",
        "prev5_turnover_mean",
        "atr14",
        "jsf_net_balance",
        "prev_jsf_net",
        *balances,
    ]
    ok = (
        np.isfinite(frame[fields]).all(axis=1)
        & frame[balances].gt(0).any(axis=1)
        & frame.close.ge(300)
        & frame.close.gt(frame.ma5)
        & frame.ma5.gt(frame.ma20)
        & frame.ret_20d.gt(0)
        & frame.turnover.ge(100_000_000)
        & frame.turnover.gt(frame.prev5_turnover_mean)
        & frame.atr14.gt(0)
        & frame.jsf_net_balance.le(frame.prev_jsf_net)
        & frame.jsf_restricted.eq(False)
        & frame.no_split_20d
        & ~frame.code5.isin(excluded)
    )
    ranked = []
    for r in frame.loc[ok].sort_values(["ret_20d", "code5"], ascending=[False, True]).itertuples():
        code = r.code5
        info = master.get(code, {})
        market = info.get("MktNm", info.get("MarketCodeName"))
        if market not in ("プライム", "スタンダード", "グロース", "Prime", "Standard", "Growth"):
            continue
        if not runner._is_domestic_common_stock(code, info):
            continue
        limit, shares, reserve = order_size(float(r.close), budget)
        if shares:
            ranked.append(
                dict(
                    code=code,
                    name=info.get("CoName", ""),
                    rank=len(ranked) + 1,
                    prev_close=float(r.close),
                    atr14=float(r.atr14),
                    limit_price=limit,
                    shares=shares,
                    required_cash=reserve,
                    sector33_code=sector(info),
                )
            )
        if len(ranked) == 5:
            break
    return ranked


def sector(info):
    value = info.get("S33", info.get("Sector33Code"))
    return str(value).zfill(4) if value is not None and str(value).zfill(4) in SECTORS else None


def reserve(ranked, positions, exits, cash):
    carried = Counter(p["sector33_code"] for p in positions if p["code5"] not in exits)
    held = {p["code5"] for p in positions}
    for row in ranked:
        code, industry = row["code"], row["sector33_code"]
        if code in held:
            status = "held"
        elif not industry or None in carried:
            status = "sector_unverified"
        elif carried[industry] >= 3:
            status = "sector_limit"
        elif row["required_cash"] > cash:
            status = "cash"
        else:
            status = "reserved"
            cash -= row["required_cash"]
            carried[industry] += 1
        row["status"] = status


class C500Entries:
    version = VERSION

    def __init__(self, project, account, window, target, now):
        self.account, self.window, self.target = account, window, target
        try:
            self.gate = market_gate(project, window, now)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, duckdb.Error) as exc:
            self.gate = dict(
                date=window[-1],
                close=None,
                sma25=None,
                bullish=None,
                issue=str(exc)[:150],
                sources=[],
            )

    @staticmethod
    def runner_type(base):
        class SignalRunner(base):
            def compute_indicators(self, history, splits):
                result = super().compute_indicators(history, splits)
                self.signal_rows = result.loc[result.date.eq(result.date.max())]
                return result

        return SignalRunner

    def complete(self, runner, state, rows, credit_ready, excluded, history):
        signal, target = self.window[-1], self.target
        client = runner.client
        master = (
            client.fetch_listed_info(signal)
            .set_index("code5", verify_integrity=True)
            .to_dict("index")
        )
        for p in state["positions"]:
            previous = self.account["positions"][p["code5"]]
            p["sector33_code"] = (
                sector(master[p["code5"]])
                if p["code5"] in master
                else previous.get("sector33_code")
            )
        reason = None
        if self.gate["bullish"] is None:
            reason = "TOPIXの25立会日分の終値を確認できない"
        elif not self.gate["bullish"]:
            reason = "TOPIX終値が25日単純移動平均以下"
        elif not credit_ready:
            reason = "日証金の期限内照合が未完了"
        budget = min(self.account["nav"] * 0.045, 400_000)
        ranked = []
        if credit_ready:
            if target in state["closed_new_order_dates"] or target in state["synced_dates"]:
                raise ValueError("c500_session_already_closed")
            if any(
                o["status"] in ("OPEN", "PENDING_CANCEL") and o["remaining_shares"] > 0
                for o in state["pending_orders"]
            ):
                raise ValueError("c500_unsettled_previous_reservation")
            # Validate the unfilled raw adjustment column before V21's legacy normalization.
            today = (
                client.fetch_daily_quotes(signal)[["code5", "AdjFactor"]]
                .rename(columns={"AdjFactor": "adj_factor"})
                .assign(date=signal)
            )
            factors = pd.concat(
                [history[["date", "code5", "adj_factor"]], today], ignore_index=True
            )
            factors = factors.loc[factors.date.isin(self.window[-21:])].copy()
            factors["unadjusted"] = pd.to_numeric(factors.adj_factor, errors="coerce").eq(1)
            valid = factors.groupby("code5").unadjusted.sum().eq(21)
            allowed = set(valid[valid].index)
            frame = runner.signal_rows.loc[runner.signal_rows.code5.isin(allowed)]
            ranked = rank_candidates(frame, master, runner, budget, excluded)
            exits = {r["コード5桁"] for r in rows if r["区分"] == "売り(成行・寄付)"}
            if reason:
                for row in ranked:
                    row["status"] = "market_closed"
            else:
                reserve(ranked, state["positions"], exits, state["cash"])
        for c in ranked:
            if c["status"] != "reserved":
                continue
            code = c["code"]
            oid = f"ORD_{target}_{code}"
            state["pending_orders"].append(
                dict(
                    order_id=oid,
                    code5=code,
                    disp_code=code[:4],
                    name=c["name"],
                    selection_date=signal,
                    target_trade_date=target,
                    selection_close=c["prev_close"],
                    selection_atr14=c["atr14"],
                    limit_price=c["limit_price"],
                    ordered_shares=c["shares"],
                    remaining_shares=c["shares"],
                    filled_shares=0,
                    status="OPEN",
                    sector33_code=c["sector33_code"],
                )
            )
            rows.append(
                {
                    "区分": f"買い#{c['rank']}(指値・寄付)",
                    "注文ID": oid,
                    "コード5桁": code,
                    "銘柄名": c["name"],
                    "株数": c["shares"],
                    "基準価格": c["prev_close"],
                    "指値または逆指値": c["limit_price"],
                    "sector33_code": c["sector33_code"],
                    "rank": c["rank"],
                }
            )
        return dict(regime=self.gate, target_budget=budget, ranked=ranked, no_buy_reason=reason)


def advance(project, book, *, start, capital, now):
    return plan_b_v21.advance(
        project, book, start=start, capital=capital, now=now, entry_profile=C500Entries
    )
