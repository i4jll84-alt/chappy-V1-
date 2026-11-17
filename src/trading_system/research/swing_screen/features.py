"""Causal price, liquidity and risk features on the exchange trading calendar."""

import hashlib

import numpy as np
import pandas as pd

MARKETS = {"0111", "0112", "0113", "0101", "0102", "0104", "0106", "0107"}
HORIZONS = (5, 10, 20, 60)


class Features:
    def __init__(self, raw, index, dates):
        self.raw = raw.set_index(["Date", "Code"]).sort_index()
        self.codes = self.raw.index.unique("Code").sort_values()

        def wide(key):
            return self.raw[key].unstack().reindex(index=dates, columns=self.codes)

        o, h, low_price, c, vol, va, factor = (
            wide(k) for k in ("O", "H", "L", "C", "Vo", "Va", "AdjFactor")
        )
        present = (
            c.gt(0)
            & o.gt(0)
            & low_price.gt(0)
            & h.ge(c)
            & h.ge(o)
            & low_price.le(c)
            & low_price.le(o)
            & factor.gt(0)
        )
        price = c.where(present)
        scale = factor.where(factor.gt(0), 1.0).cumprod()
        normalized = price / scale
        high = h.where(present) / scale
        low = low_price.where(present) / scale
        volume = vol.where(vol.gt(0)) * scale
        turnover = va.where(va.gt(0))
        unknown_action = (factor.ne(1) & ~wide("ExRT").isin(["1", "2"])) | (
            factor.isna() & c.notna()
        )
        range5 = ((h - low_price) / price).where(present & vol.gt(0) & va.gt(0)).rolling(5).median()
        rights = factor.ne(1) | (~wide("ExRT").isin(["", None, "0"]) & wide("ExRT").notna())
        values = {
            "C": price,
            "value5": turnover.rolling(5).median(),
            "range5": range5,
            "clean5": rights.rolling(5).max().eq(0),
            "value20": turnover.rolling(20).median(),
            "value60": turnover.rolling(60).median(),
            "volume_cv": volume.rolling(20).std() / volume.rolling(20).mean(),
            "volume_ratio": volume / volume.shift(1).rolling(20).mean(),
            "cost": price * 100 * 1.0005 + 10,
        }
        for n in HORIZONS:
            valid = (normalized.rolling(n + 1).count() == n + 1) & unknown_action.rolling(
                n
            ).max().eq(0)
            mom = (normalized / normalized.shift(n) - 1).where(valid)
            values["m" + str(n)] = mom
            values["rs" + str(n)] = mom.sub(index.C / index.C.shift(n) - 1, axis=0)
        for n in (20, 60):
            key = str(n)
            values["skip" + key] = (normalized.shift(5) / normalized.shift(n) - 1).where(
                values["m" + key].notna()
            )
            prior = high.shift(1).rolling(n).max()
            values["break" + key] = (normalized / prior - 1).where(values["m" + key].notna())
            values["below_high" + key] = normalized / high.rolling(n).max() - 1
        for n in (10, 20, 60):
            ma = normalized.rolling(n).mean()
            values["ma" + str(n)] = ma * scale
            values["ma_slope" + str(n)] = ma / ma.shift(1) - 1
            values["dev" + str(n)] = normalized / ma - 1
        change = normalized.diff().abs().rolling(20).sum()
        values["eff20"] = (
            (normalized - normalized.shift(20)).abs() / change.where(change > 0)
        ).where(values["m20"].notna())
        tr = np.maximum(
            high - low,
            np.maximum((high - normalized.shift(1)).abs(), (low - normalized.shift(1)).abs()),
        )
        values["atr"] = tr.rolling(14).mean() * scale
        values["atr_frac"] = values["atr"] / price
        values["dev_atr"] = (price - values["ma20"]) / values["atr"].where(values["atr"] > 0)
        values["volatility20"] = normalized.pct_change(fill_method=None).rolling(20).std()
        self.values = values
        iatr = (
            np.maximum(
                index.H - index.L,
                np.maximum((index.H - index.C.shift()).abs(), (index.L - index.C.shift()).abs()),
            )
            .rolling(14)
            .mean()
        )
        m20 = index.C.rolling(20).mean()
        m60 = index.C.rolling(60).mean()
        self.regime = pd.Series(
            np.select(
                [
                    iatr / index.C >= 0.02,
                    (index.C > m20) & (m20 > m60),
                    (index.C < m20) & (m20 < m60),
                ],
                ["high_vol", "up", "down"],
                default="sideways",
            ),
            index=index.index,
        ).where(m60.notna(), "unknown")
        self.index20 = index.C / index.C.shift(20) - 1

    def day(self, day):
        master = self.raw.loc[day, ["CoName", "Mkt", "S33", "ProdCat"]]
        f = pd.DataFrame({k: v.loc[day] for k, v in self.values.items()}).join(master, how="inner")
        f["domestic"] = f.ProdCat.eq("011") & f.index.str.endswith("0") & f.Mkt.isin(MARKETS)
        sample = f.m20.where(f.domestic & ~f.S33.isin(["", "99", "9999"]))
        grouped = sample.groupby(f.S33)
        f["sector20"] = grouped.transform("median")
        f["sector_rank"] = grouped.rank(ascending=False, method="min")
        f["sector_relative"] = f.m20 - f.sector20
        f["sector_vs_index"] = f.sector20 - self.index20.at[day]
        f["surge"] = f.m5.ge(0.10).where(f.m5.notna())
        return f


def eligible(f, cfg, method, floor=None):
    cost = f.cost if method == "A" else f.C * 100 * (1 + cfg["rate"]) + cfg["fee"]
    base = f.domestic & f.C.gt(0) & cost.le(cfg["cap_yen"])
    if method == "A":
        return base & (f.index != "72030") & f.clean5 & f.range5.notna() & f.value5.notna()
    value = f["value" + str(cfg["liquidity_window"])]
    threshold = cfg["liquidity"] if floor is None else floor
    if threshold > 0:
        base = base & value.ge(threshold)
    if cfg["max_volume_cv"] is not None:
        base = base & f.volume_cv.le(cfg["max_volume_cv"])
    return base


def rankings(f, cfg, day, *, variants=True, native_only=False):
    limit = cfg["display"]

    def sorted_codes(mask, cols):
        return f.loc[mask].sort_values(cols, ascending=False, kind="stable").index[:limit].tolist()

    def rank_a(mask):
        liquid = f.loc[mask].sort_values("value5", ascending=False, kind="stable").head(100)
        return (
            liquid.sort_values(["range5", "value5"], ascending=False, kind="stable")
            .index[:limit]
            .tolist()
        )

    f = f.sort_index().copy()
    a = eligible(f, cfg, "A")
    base = eligible(f, cfg, "B")
    d = f.m20.gt(0) & f.m60.gt(0) & f.C.gt(f.ma20) & f.ma20.gt(f.ma60) & f.ma_slope20.gt(0)

    def combo(mask):
        x = f.loc[mask, ["m20", "m60"]].rank(pct=True).mean(axis=1)
        return x.sort_values(ascending=False, kind="stable").index[:limit].tolist()

    out = {}

    def four(mask_a, mask, suffix):
        out["A" + suffix] = rank_a(mask_a)
        out["B" + suffix] = sorted_codes(mask & f.m20.gt(0), ["m20"])
        out["C" + suffix] = sorted_codes(mask & f.rs20.gt(0), ["rs20"])
        out["D" + suffix] = combo(mask & d)

    four(a, base, "")
    if native_only:
        return out
    # Identical information coverage, with each method's own signal still applied.
    common = a & base & f[["rs20", "m60", "ma20", "ma60", "ma_slope20"]].notna().all(axis=1)
    four(common, common, "_common")
    pool = f.loc[base & f.m20.notna() & f.value20.notna(), ["value20"]].copy()
    if len(pool):
        pool["bin"] = np.minimum(
            (pool.value20.rank(method="first", pct=True) * 10 - 1e-9).astype(int), 9
        )
        ties = {c: hashlib.blake2s((day + c).encode(), digest_size=8).digest() for c in pool.index}
        groups = {k: iter(sorted(g.index, key=ties.get)) for k, g in pool.groupby("bin")}
        control = [next(groups[pool.at[c, "bin"]]) for c in out["B"] if c in pool.index]
    else:
        control = []
    out["matched"] = control
    if variants:
        for threshold in cfg["liquidity_tests"]:
            if threshold != cfg["liquidity"]:
                mask = eligible(f, cfg, "B", threshold)
                for key, flag in [("B", f.m20.gt(0)), ("C", f.rs20.gt(0))]:
                    out[key + "_" + str(threshold)] = sorted_codes(mask & flag, ["m20"])
                out["D_" + str(threshold)] = combo(mask & d)
        for name, col, mask in [
            ("B60", "m60", f.m60.gt(0)),
            ("skip5", "skip20", f.skip20.gt(0)),
            ("high20", "break20", f.break20.gt(0)),
            ("high60", "break60", f.break60.gt(0)),
            ("efficiency", "eff20", f.m20.gt(0) & f.eff20.notna()),
            ("industry", "sector_relative", f.sector_relative.gt(0)),
        ]:
            out[name] = sorted_codes(base & mask, [col])
    return out


def annotate(f, chosen, cfg):
    result = f.copy()
    order = rankings(f, dict(cfg, display=len(f)), None, native_only=True)
    result["swing_cost"] = f.C * 100 * (1 + cfg["rate"]) + cfg["fee"]
    for method in ("A", "B", "C", "D"):
        result["eligible_rank_" + method] = result.index.map(
            {c: i for i, c in enumerate(order[method], 1)}
        )
        ranked = {c: i for i, c in enumerate(chosen[method], 1)}
        result["rank_" + method] = result.index.map(ranked)
        mask = eligible(f, cfg, method)
        cost = f.cost if method == "A" else result.swing_cost
        result["reason_" + method] = np.select(
            [
                ~f.domestic,
                f.cost.isna(),
                cost.gt(cfg["cap_yen"]),
                ~mask,
                result["rank_" + method].notna(),
            ],
            ["market_or_security", "price_missing", "lot_cost", "liquidity_or_data", "selected"],
            default="signal_or_rank",
        )
        if method != "A":
            history = f["m60" if method == "D" else "m20"]
            result.loc[mask & history.isna(), "reason_" + method] = "missing_feature_history"
        reason = "reason_" + method
        chosen_mask = result["rank_" + method].notna()
        if method == "A":
            result.loc[mask & ~chosen_mask, reason] = "outside_turnover100_or_range_rank"
            result.loc[f.index == "72030", reason] = "diagnostic_control"
            result.loc[chosen_mask, reason] = "5日売買代金上位100・値幅率順"
        else:
            score = f.rs20 if method == "C" else f.m20
            result.loc[mask & score.notna() & score.le(0), reason] = "nonpositive_return_or_excess"
            result.loc[mask & score.gt(0) & ~chosen_mask, reason] = "outside_selected_rank"
            if method == "D":
                valid = f.m60.notna() & f.ma_slope20.notna()
                trend = f.m60.gt(0) & f.C.gt(f.ma20) & f.ma20.gt(f.ma60) & f.ma_slope20.gt(0)
                result.loc[mask & valid & ~trend, reason] = "trend_filter"
                result.loc[mask & ~valid, reason] = "missing_60day_or_trend_history"
            result.loc[chosen_mask, reason] = {
                "B": "20日騰落率が正・上昇率順",
                "C": "TOPIX比20日超過が正・相対強度順",
                "D": "20・60日上昇と移動平均条件・複合順位",
            }[method]
    return result
