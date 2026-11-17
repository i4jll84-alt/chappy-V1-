"""Calendar-period, regime, concentration and paired cohort statistics."""

import numpy as np
import pandas as pd

from .prepare import load
from .study import CORE


def periods(days, cfg):
    def next_day(day):
        return str((pd.Timestamp(day) + pd.Timedelta(days=1)).date())

    first, last = days[0], days[-1]
    result = {
        "full": (first, last),
        "development": (first, cfg["development_end"]),
        "selection": (next_day(cfg["development_end"]), cfg["selection_end"]),
        "final_known_history": (next_day(cfg["selection_end"]), last),
    }
    for n in (10, 5, 3):
        start = str((pd.Timestamp(last) - pd.DateOffset(years=n)).date())
        result[str(n) + "y"] = (max(first, start), last)
    result.update({y: (y + "-01-01", y + "-12-31") for y in sorted({d[:4] for d in days})})
    return result


def load_frames(out, kind):
    frames = [pd.read_parquet(p) for p in sorted(out.glob("*-" + kind + ".parquet"))]
    nonempty = [f for f in frames if not f.empty]
    return pd.concat(nonempty, ignore_index=True) if nonempty else pd.DataFrame()


def interval(x, sells, cap, base, base_ix, initial_fees):
    nav = x.nav.to_numpy()
    peak = np.maximum.accumulate(np.r_[base, nav])[1:]
    pnl = nav[-1] - base
    ret = nav[-1] / base - 1
    ps = sells.pnl if len(sells) else pd.Series(dtype=float)
    wins = ps[ps > 0]
    losses = ps[ps < 0]
    bycode = sells.groupby("code").pnl.sum() if len(sells) else ps
    bysector = sells.groupby("sector").pnl.sum() if len(sells) else ps
    return dict(
        net=pnl,
        ret=ret,
        excess=ret - (x.topix.iat[-1] / base_ix - 1),
        mdd=np.min(nav / peak - 1),
        gross=pnl + x.fees.iat[-1] - initial_fees,
        closed=len(ps),
        win_rate=(ps > 0).mean(),
        avg_win=wins.mean(),
        avg_loss=losses.mean(),
        payoff=-wins.mean() / losses.mean() if len(losses) else None,
        pf=-wins.sum() / losses.sum() if len(losses) else None,
        invested=(x.market / x.nav).mean(),
        uncertainty_days=int(x.uncertain.sum()),
        stale_days=int((x.stale > 0).sum()),
        last_open=int(x.positions.iat[-1]),
        std_daily=x.nav.pct_change().std(),
        capital=cap,
        largest_winner=bycode.idxmax() if len(bycode) else None,
        top5_profit_share=bycode.nlargest(5).clip(lower=0).sum() / bycode.clip(lower=0).sum()
        if (bycode > 0).any()
        else None,
        worst_stock=bycode.idxmin() if len(bycode) else None,
        worst_sector=bysector.idxmin() if len(bysector) else None,
    )


def block_ci(values, horizon):
    v = np.asarray(values, float)
    n = len(v)
    if n < 4 * horizon:
        return [None, None]
    rng = np.random.default_rng(20261117)
    width = max(20, horizon)
    blocks = np.lib.stride_tricks.sliding_window_view(v, width).mean(axis=1)
    draws = rng.choice(blocks, (500, max(1, n // width))).mean(axis=1)
    return np.quantile(draws, [0.025, 0.975]).tolist()


def summarize(history, out):
    cfg = load(out / "checks.json")["config"]
    manifest = load(history / "manifest.json")
    days = manifest["days"]
    cases = load(out / "cases.json")
    nav = load_frames(out, "nav")
    trades = load_frames(out, "trades")
    if trades.empty:
        trades = pd.DataFrame(columns=["case", "date", "side", "pnl", "code", "sector"])
    labels = load_frames(out, "labels")
    if labels.empty:
        raise ValueError("no completed holding horizon in this study")
    nav["daily_return"] = nav.groupby("case").nav.pct_change().fillna(0.0)
    ps = periods(days, cfg)
    stats = []
    segment = []
    groups = trades.loc[trades.side.eq("sell")].groupby("case")
    for case, x in nav.groupby("case", sort=False):
        x = x.sort_values("date").reset_index(drop=True)
        cap = cases[case]["capital"]
        sales = groups.get_group(case) if case in groups.groups else trades.iloc[:0]
        for label, (start, end) in ps.items():
            y = x.loc[x.date.between(start, end)]
            if y.empty:
                continue
            pos = y.index[0]
            base = x.nav.iat[pos - 1] if pos else cap
            ix = x.topix.iat[pos - 1] if pos else x.topix.iat[0]
            fees = x.fees.iat[pos - 1] if pos else 0
            st = interval(y, sales.loc[sales.date.between(start, end)], cap, base, ix, fees)
            stats.append(
                dict(case=case, period=label, first=y.date.iat[0], last=y.date.iat[-1], **st)
            )
        for reg, y in x.groupby("regime"):
            segment.append(
                dict(
                    case=case,
                    regime=reg,
                    days=len(y),
                    mean_daily_return=y.daily_return.mean(),
                    std=y.daily_return.std(),
                )
            )
    table = pd.DataFrame(stats)
    table.to_csv(out / "portfolio.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(segment).to_csv(out / "regimes.csv", index=False, encoding="utf-8-sig")
    confidence = []
    periods_table = []
    # Selected close precedes entry. Only labels exiting BEFORE the next segment count.
    pos = {d: i for i, d in enumerate(days)}
    for label, (lo, hi) in ps.items():
        period_days = [d for d in days if lo <= d <= hi]
        if not period_days:
            continue
        subset = labels.loc[
            labels.entry.between(lo, hi)
            & (labels.entry.map(pos) + labels.hold <= pos[period_days[-1]])
        ]
        if label in ("selection", "final_known_history"):
            subset = subset.loc[subset.entry.map(pos) >= pos[period_days[0]] + cfg["embargo"]]
        for (method, h), g in subset.groupby(["method", "hold"]):
            periods_table.append(
                dict(
                    period=label,
                    method=method,
                    hold=h,
                    planned=g.planned.sum(),
                    observed=g.observed.sum(),
                    missing=g.missing.sum(),
                    mean_gross=g.gross.mean(),
                    mean_net=g.net.mean(),
                    mean_excess=g.excess.mean(),
                    wins=g.wins.sum(),
                    avg_win=g.gain.sum() / g.wins.sum() if g.wins.sum() else None,
                    avg_loss=g.loss.sum() / g.nloss.sum() if g.nloss.sum() else None,
                    sd=g.excess.std(),
                    decision_days=g.excess.count(),
                )
            )
        if label in ("full", "final_known_history"):
            for h, part in subset.groupby("hold"):
                panel = part.pivot(index="entry", columns="method", values="net")
                for m in CORE:
                    diff = (panel[m] - panel["B"]).dropna()
                    confidence.append(
                        dict(
                            period=label,
                            hold=h,
                            method=m,
                            comparison="B",
                            mean=diff.mean(),
                            lower=block_ci(diff, h)[0],
                            upper=block_ci(diff, h)[1],
                            paired=len(diff),
                        )
                    )
    pd.DataFrame(periods_table).to_csv(out / "cohorts.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(confidence).to_csv(out / "paired-ci.csv", index=False, encoding="utf-8-sig")
    labels.groupby(["method", "hold", "regime"])[["net", "excess", "hot", "normal"]].mean().to_csv(
        out / "signal-regimes.csv", encoding="utf-8-sig"
    )
    attribution = (
        trades.loc[trades.side.eq("sell")]
        .groupby(["case", "code", "sector"], dropna=False)
        .pnl.agg(["size", "sum"])
    )
    attribution.to_parquet(out / "concentration.parquet")
    return table


def markdown(table, cfg, out):
    cap = cfg["primary_capital"]
    r = table.set_index(["case", "period"])

    hold = cfg["primary_hold"]
    checks = load(out / "checks.json")

    def row(title, metric, h=None, period="full", style="+.2%"):
        h = hold if h is None else h
        if (f"A:{cap}:{h}", period) not in r.index:
            return "|" + title + "|" + "未検証|" * 4
        return (
            "|"
            + title
            + "|"
            + "|".join(format(r.loc[(f"{m}:{cap}:{h}", period), metric], style) for m in CORE)
            + "|"
        )

    lines = [
        "# 順張りスイングの価格ベースの研究結果",
        "",
        f"基準は{cap / 10000:g}万円の独立口座。翌寄付買い・固定日数後の寄付売り。"
        "100株単位、売却代金T+2。残高は期末終値評価。"
        f"{checks['first']}～{checks['last']}、{checks['sessions']:,}営業日の既知履歴。",
        "",
        "|指標|A 値幅|B 上昇率|C 対TOPIX|D 複合|",
        "|---|---:|---:|---:|---:|",
    ]
    for h in cfg["holds"]:
        lines.append(row(f"{h}日保有・費用後累積収益率", "ret", h))
    lines.extend(
        [
            row(f"{hold}日・対TOPIX差", "excess"),
            row(f"{hold}日・最大ドローダウン", "mdd"),
            row(f"{hold}日・決済勝率", "win_rate"),
            row(f"{hold}日・決済件数", "closed", style=".0f"),
            row(
                f"{cfg['selection_end']}より後の口座騰落率（既知履歴）",
                "ret",
                period="final_known_history",
            ),
            row(f"{hold}日・未解決の権利等を含む評価日数", "uncertainty_days", style=".0f"),
            "|完全未使用の将来成績|未検証|未検証|未検証|未検証|",
            "",
            "未解決の権利・銘柄消失は建玉を残し、直前評価額を暫定表示する。該当日数が正の口座の成績・下落率は暫定値。配当受取・実測コスト・売買成立順の確証が欠け、配当込み・税引後の成績は未検証。",
            "",
            f"## 年別の{hold}日保有口座騰落率",
            "",
            "|年|A|B|C|D|",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for y in sorted(set(table.period)):
        if str(y).isdecimal():
            lines.append(row(y, "ret", period=y))
    lines += [
        "",
        "年別は連続口座の騰落率。条件固定で順送りし、境界を跨ぐラベルを除外。"
        f"条件比較・最終区間冒頭は{cfg['embargo']}立会日除外。"
        f"履歴開始 {cfg['earliest_archive']}、共通評価開始 {checks['first']}。"
        "市場再編前は当時の対応市場で評価。後日一括取得したデータの当時の受領・訂正履歴は未検証。",
        "",
        "portfolio.csv に元本・期間別の資金効率と損益、cohorts.csv に独立ラベルを保存。"
        "paired-ci.csv は20日ブロックの参考95%区間、regimes.csv は相場別の成績。"
        "銘柄・業種別損益は concentration.parquet。多数比較を踏まえ自動採用は行わない。",
    ]
    from .report_detail import details

    lines.extend(details(table, cfg, out))
    (out / "結果.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
