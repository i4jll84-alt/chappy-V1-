"""Read-only desktop representation of today's decision and the paper audit."""


def describe(s):
    if "accounts" in s:
        return "\n\n".join(
            f"【{int(capital):,}円口座】\n{describe(account)}"
            for capital, account in s["accounts"].items()
        )
    a = s["audit"]
    rec = s["decision"]
    lines = [
        s["mode"] + (" ｜ 有効" if s["enabled"] else " ｜ オフ"),
        f"独立元本 {s['capital']:,}円　現金 {s['cash']:,.0f}円　保有 {len(s['positions'])}銘柄",
    ]
    if s["stopped"]:
        lines.append("V1のペーパー停止中")
    elif not s["enabled"]:
        lines.append("開始するには起動画面の手法名にチェックを入れてください。")
    elif not rec:
        lines.append("当日の判定はまだありません。引け後に「当日分を保存」を実行してください。")
    elif rec["state"] != "ready":
        lines.append("予定生成停止　" + rec.get("reason", rec["state"]))
    else:
        n = rec["counts"]
        lines.extend(
            [
                f"判定 {rec['date']}　寄付予定 {rec['order_date']}　入力締切 {rec['cutoff']}",
                f"データ完備 {n['data_ready']:,}　"
                f"有効信用 {n['margin_known']:,}　流動性等適用後 {n['eligible']:,}",
                f"買付余力 {rec['available']:,.0f}円　未受渡 {rec['locked']:,.0f}円　"
                f"保有可能な空き枠 {rec['free_slots']}",
                f"入力原本 {len(rec['inputs'])}件　"
                f"決算予定 {rec['earnings_input'].get('source') or '当日原本なし'}",
            ]
        )
        for side, title in (("SELL", "売り予定"), ("BUY", "買い予定")):
            lines.extend(["", title])
            rows = [o for o in rec["orders"] if o["side"] == side]
            lines.extend(
                f"{o['Code']}　{o['company']}　{o['shares']:,}株　{o['reason']}"
                + ("　売り成立条件付き" if o["conditional_on_sales"] else "")
                + ("　" + o["warning"] if o.get("warning") else "")
                for o in rows
            )
            if not rows:
                lines.append("該当なし")
        lines.append(f"候補 {len(rec['candidates'])}銘柄")
        lines.extend(
            f"{r['rank']}位　{r['Code']}　{r['decision']}　{r['earnings_source']}"
            for r in rec["candidates"]
        )
        lines.extend(rec["alerts"])
    if s.get("due"):
        lines.append("本日寄付の照合待ち")
        for o in s["due"]["orders"]:
            lines.append(f"{o['side']}　{o['Code']}　{o['shares']:,}株　{o['reason']}")
    for row in s["settlements"]:
        lines.append(f"受渡 {row['date']}　{row['amount']:,.0f}円")
    lines.extend(
        [
            "",
            "口座監査",
            f"仮想約定 {a['fills']}件　決済 {a['closed']}件　実現損益 {a['realized']:+,.0f}円",
            "モデル滑り　買い・売り・片道平均とも10bp　手数料は約定代金の5bp",
            "実測滑り（買い・売り・片道平均）と想定コストとの差額　未観測　警戒ライン +25bp",
        ]
    )
    for name, key, percent in (
        ("約定ルール遵守率", "compliance", True),
        ("PF", "pf", False),
        ("勝率", "win_rate", True),
        ("ペイオフ", "payoff", False),
        ("実現損益MDD", "realized_mdd", True),
    ):
        v = a[key]
        lines.append(
            name + "　" + ("未算出" if v is None else f"{v:.2%}" if percent else f"{v:.3f}")
        )
    return "\n".join(lines)
