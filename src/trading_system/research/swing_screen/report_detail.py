"""Reproduce the comparison tables and their coverage from the saved replay."""

import pandas as pd

from .study import CORE


def details(portfolio, cfg, out):
    from .report import load_frames

    cap = cfg["primary_capital"]
    hold = cfg["primary_hold"]
    cases = [f"{m}:{cap}:{hold}" for m in CORE]
    portfolio = portfolio.loc[portfolio.period.eq("full")].set_index("case")
    cohorts = pd.read_csv(out / "cohorts.csv")
    groups = cohorts.loc[cohorts.period.eq("full")].set_index(["method", "hold"])
    nav = load_frames(out, "nav")
    nav = nav.loc[nav.case.isin(cases)].sort_values("date")
    unknown = nav.loc[nav.uncertain | nav.stale.gt(0), "date"]
    reliable = nav if unknown.empty else nav.loc[nav.date < unknown.min()]
    lines = []

    def table(title, header, rows):
        lines.extend(
            [
                "",
                "## " + title,
                "",
                "|" + "|".join(header) + "|",
                "|" + "|".join("---" for _ in header) + "|",
            ]
        )
        lines.extend("|" + "|".join(map(str, row)) + "|" for row in rows)

    def fmt(v):
        return f"{v:+.2%}" if pd.notna(v) else "未検証"

    def symbol(m):
        return f"{m}:{cap}:{hold}"

    rows = []
    for h in cfg["holds"]:
        for label, col in [("費用後平均", "mean_net"), ("同期間TOPIXとの差", "mean_excess")]:
            rows.append(
                [
                    f"{h}日・{label}",
                    *[
                        fmt(groups.at[(m, h), col]) if (m, h) in groups.index else "未検証"
                        for m in CORE
                    ],
                ]
            )
    table("候補単位の翌寄付からの騰落率", ["保有期間・指標", *CORE], rows)
    lines += [
        "",
        "日ごとに観測できた候補を等ウェイトにし、判定日を等ウェイトで平均。超過は同期間のTOPIX価格指数との％ポイント差。欠測銘柄や末尾の保有期間未到来は別集計。",
    ]
    codes = [*CORE, *(m + "_common" for m in CORE), "matched"]
    table(
        f"{hold}日・母集団と観測範囲",
        ["方式", "観測できた延べ件数", "評価不能", "費用後平均", "対TOPIX差"],
        [
            [
                m,
                int(groups.at[(m, hold), "observed"]),
                int(groups.at[(m, hold), "missing"]),
                fmt(groups.at[(m, hold), "mean_net"]),
                fmt(groups.at[(m, hold), "mean_excess"]),
            ]
            for m in codes
            if (m, hold) in groups.index
        ],
    )
    lines.extend(
        [
            "",
            "common は全方式に必要な情報を同時に観測できた銘柄。"
            "matched はBと当日の流動性分布を合わせた比較群。",
        ]
    )
    if len(reliable):
        last = reliable.groupby("case").tail(1).set_index("case")
        ix = reliable.iloc[0].topix
        table(
            "未解決の保有権利・評価欠測が初めて発生する前の共通期間",
            ["評価最終日", *CORE, "TOPIX"],
            [
                [
                    last.date.iloc[0],
                    *[fmt(last.at[c, "nav"] / cap - 1) for c in cases],
                    fmt(last.topix.iloc[0] / ix - 1),
                ]
            ],
        )
        lines.extend(
            [
                "",
                "同じ開始日からの累積。配当・税は含まない。以下の全期間表には権利処理等が未確定の口座を含む。",
            ]
        )
    table(
        f"{hold}日保有・元本別の費用後累積収益率（未確定権利を含む暫定値）",
        ["元本", *CORE],
        [
            [f"{c / 10000:g}万円", *[fmt(portfolio.at[f"{m}:{c}:{hold}", "ret"]) for m in CORE]]
            for c in cfg["capital_levels"]
        ],
    )
    reg = pd.read_csv(out / "signal-regimes.csv")
    tbl = reg.loc[reg.hold.eq(hold)].set_index(["method", "regime"])
    table(
        f"{hold}日・相場別の候補の対TOPIX差",
        ["相場", *CORE],
        [
            [
                label,
                *[fmt(tbl.at[(m, g), "excess"]) if (m, g) in tbl.index else "未検証" for m in CORE],
            ]
            for g, label in [
                ("up", "上昇"),
                ("down", "下降"),
                ("sideways", "横ばい"),
                ("high_vol", "高変動"),
            ]
        ],
    )
    table(
        "執行・リスク条件の全期間比較（口座騰落率、暫定値を含む）",
        ["条件", *CORE],
        [
            [label, *[fmt(portfolio.at[m + s, "ret"]) for m in CORE]]
            for s, label in [
                (f":{cap}:{hold}", "基準"),
                (":limit", "指値"),
                (":atr", "ATR損切り"),
                (":trail", "ATR追随"),
                (":risk", "ATRによる数量制限"),
                (":trend", "20日線割れ"),
                (":sector_free", "業種銘柄数制限解除"),
                (":stress", "費用ストレス"),
                ("_before_earnings", "公表済み決算予定前"),
                ("_after_earnings", "発表後の上昇確認"),
            ]
        ],
    )
    lines += [
        "",
        "決算予定履歴の空白は取引機会を減らす。寄付・指値は日足による仮定約定。場中継続は受信済み分足と売り気配のある銘柄・時間だけを観測でき、長期比較は未検証。",
    ]
    table(
        "銘柄・業種への依存（主条件・実現損益）",
        ["指標", *CORE],
        [
            [
                "利益の上位5銘柄の割合",
                *[fmt(portfolio.at[symbol(m), "top5_profit_share"]) for m in CORE],
            ],
            [
                "損失が最大の銘柄（J-Quantsコード）",
                *[str(portfolio.at[symbol(m), "worst_stock"]) for m in CORE],
            ],
            [
                "損失が最大の33業種（コード）",
                *[str(portfolio.at[symbol(m), "worst_sector"]) for m in CORE],
            ],
        ],
    )
    ci = pd.read_csv(out / "paired-ci.csv")
    same = ci.loc[ci.period.eq("final_known_history") & ci.hold.eq(hold) & ci.method.eq("D")]
    if len(same):
        q = same.iloc[0]
        lines.extend(
            [
                "",
                f"最終区間のDとBの費用後平均差は {100 * q['mean']:+.3f} ポイント。"
                f"95%区間は {100 * q.lower:+.3f} ～ {100 * q.upper:+.3f} ポイント"
                f"（同時に評価できた{int(q.paired)}日）。",
            ]
        )
    lines.extend(
        [
            "",
            "採用判断では独立候補の対指数差、共通母集団、未解決建玉、費用ストレスを合わせて確認する。方式・保有期間・停止条件の変更は別版の研究とし、翌寄付前に固定した模擬選定で前向きに評価する。",
        ]
    )
    return lines
