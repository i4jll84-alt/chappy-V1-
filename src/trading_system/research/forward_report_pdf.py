"""Standalone Japanese PDF rendering for immutable forward-report packets.

This module deliberately imports no trading-system modules. Run it with a Python
runtime containing ReportLab; it reads only the supplied packet and writes reports
and vector charts below the explicit output directory.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab import rl_config
from reportlab.graphics import renderSVG
from reportlab.graphics.shapes import Circle, Drawing, Line, PolyLine, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

PLAN_ORDER = ("A", "B", "C", "A9", "A10")
PAGE_SIZE = landscape(A4)
MARGIN = 32
CONTENT_WIDTH = PAGE_SIZE[0] - 2 * MARGIN
INK = colors.HexColor("#172A3A")
MUTED = colors.HexColor("#526674")
RULE = colors.HexColor("#D4DFE5")
PALE = colors.HexColor("#F1F5F8")
RED = colors.HexColor("#BE2635")
BLUE = colors.HexColor("#1263AF")
TOPIX = colors.HexColor("#00836E")
FONT = "ForwardMeiryo"
BOLD = "ForwardMeiryoBold"
PERIOD_LABELS = {"day": "日次", "week": "週次", "year": "年次"}
STATUS_LABELS = {
    "complete": "集計確認済み",
    "partial": "一部未確認",
    "unavailable": "未確認",
    "conditional": "条件付き買付候補",
    "skip_already_held": "保有中・追加見送り",
    "ready": "買付候補",
    "planned": "買付予定",
    "pending": "保留",
    "pending_funds": "資金待ち",
    "insufficient_funds": "資金不足",
    "already_held": "保有中・追加見送り",
    "canceled_stopped": "停止により取消",
    "skipped_already_held": "保有中・追加見送り",
    "skip": "見送り",
    "skipped": "見送り",
    "blocked": "見送り",
    "no_signal": "該当なし",
    "paper": "ペーパー",
}


def register_fonts() -> None:
    """Embed the installed TrueType Japanese faces, including TTC subfont 0."""
    if FONT in pdfmetrics.getRegisteredFontNames():
        return
    font_dir = Path("C:/Windows/Fonts")
    regular = font_dir / "meiryo.ttc"
    bold = font_dir / "meiryob.ttc"
    if not regular.is_file() or not bold.is_file():
        raise RuntimeError("日本語フォント Meiryo / Meiryo Bold が見つかりません。")
    pdfmetrics.registerFont(TTFont(FONT, str(regular), subfontIndex=0))
    pdfmetrics.registerFont(TTFont(BOLD, str(bold), subfontIndex=0))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=BOLD, italic=FONT, boldItalic=BOLD)


def styles() -> dict[str, ParagraphStyle]:
    base = dict(fontName=FONT, fontSize=9, leading=13, textColor=INK, wordWrap="CJK")
    return {
        "body": ParagraphStyle("body", **base),
        "small": ParagraphStyle("small", **(base | {"textColor": MUTED})),
        "cell": ParagraphStyle("cell", **(base | {"leading": 12})),
        "right": ParagraphStyle("right", **(base | {"alignment": TA_RIGHT, "leading": 12})),
        "center": ParagraphStyle("center", **(base | {"alignment": TA_CENTER})),
        "head": ParagraphStyle(
            "head", **(base | {"fontName": BOLD, "fontSize": 11, "leading": 15})
        ),
        "title": ParagraphStyle(
            "title", **(base | {"fontName": BOLD, "fontSize": 23, "leading": 30})
        ),
        "subtitle": ParagraphStyle(
            "subtitle", **(base | {"fontSize": 11, "leading": 16, "textColor": MUTED})
        ),
        "section": ParagraphStyle(
            "section",
            **(
                base
                | {
                    "fontName": BOLD,
                    "fontSize": 11,
                    "leading": 16,
                    "spaceBefore": 6,
                    "keepWithNext": True,
                }
            ),
        ),
    }


STYLES: dict[str, ParagraphStyle] = {}


def value_text(value: Any) -> str:
    if value is None or value == "":
        return "未確認"
    if isinstance(value, dict):
        return " / ".join(f"{key}: {value_text(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return " / ".join(value_text(item) for item in value)
    return str(value)


def p(value: Any, style: str = "body", raw: bool = False) -> Paragraph:
    text = value_text(value)
    if not raw:
        text = escape(text).replace("\n", "<br/>")
    return Paragraph(text, STYLES[style])


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def money(value: Any, signed: bool = False, decimals: int = 2) -> str:
    num = number(value)
    if num is None:
        return "未算定"
    return f"{num:+,.{decimals}f}" if signed else f"{num:,.{decimals}f}"


def percent(value: Any, suffix: str = "%") -> str:
    num = number(value)
    return "未算定" if num is None else f"{num:+.2f}{suffix}"


def ratio(value: Any) -> str:
    if value in ("Infinity", "infinity", "inf", "+inf"):
        return "∞（損失なし）"
    num = number(value)
    return "未算定" if num is None else f"{num:.3f}"


def signed_cell(value: Any, formatter=money) -> Paragraph:
    num = number(value)
    label = formatter(value)
    color = RED if num is not None and num > 0 else BLUE if num is not None and num < 0 else INK
    return p(f'<font color="{color.hexval()}">{escape(label)}</font>', "right", raw=True)


def first(mapping: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def iso_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def plan_meta(plan: dict) -> dict:
    return first(plan, "registry", "metadata", default={}) or {}


def plan_name(key: str, plan: dict) -> str:
    meta = plan_meta(plan)
    label = first(plan, "label", "name", default=first(meta, "label", "name", "strategy_name"))
    return f"プラン{key}" if not label else f"プラン{key}  |  {label}"


def metrics(plan: dict, period: str) -> dict:
    return plan.get("periods", {}).get(period) or {}


def period_bounds(packet: dict, period: str) -> tuple[str, str]:
    asof = date.fromisoformat(packet["asof"])
    if period == "day":
        start = asof
    elif period == "week":
        start = asof - timedelta(days=asof.weekday())
    else:
        start = asof.replace(month=1, day=1)
    return start.isoformat(), asof.isoformat()


def table(rows: list, widths: list[float], header: bool = True) -> Table:
    result = Table(rows, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, -1), (-1, -1), 0.45, RULE),
    ]
    if header:
        commands += [
            ("BACKGROUND", (0, 0), (-1, 0), PALE),
            ("LINEBELOW", (0, 0), (-1, 0), 0.7, RULE),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFCFD")]),
        ]
    result.setStyle(TableStyle(commands))
    return result


def compact_table(rows: list, widths: list[float]) -> Table:
    result = table(rows, widths)
    result.setStyle(
        TableStyle([("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)])
    )
    return result


def section(title: str, content: list) -> list:
    return [KeepTogether([p(title, "section"), Spacer(1, 4), content[0]]), *content[1:]]


def metric_reason(metric: dict, name: str) -> str:
    reason = first(metric, f"{name}_reason", default="")
    if not reason and isinstance(metric.get("reasons"), dict):
        reason = metric["reasons"].get(name, "")
    translations = {
        "fewer_than_two_returns_or_zero_variance": "観測数不足または変動ゼロ",
        "no_closed_losses": "損失取引なし",
        "no_closed_trades_or_gains": "決済取引または利益なし",
        "no_closed_trades": "決済取引なし",
        "no_losses": "損失取引なし",
        "zero_volatility": "日次変動がゼロ",
        "insufficient_observations": "観測数不足",
        "insufficient_returns": "観測数不足",
        "no_returns": "日次リターンなし",
    }
    return translations.get(str(reason), value_text(reason) if reason else "")


def ratio_metric(metric: dict, name: str) -> str:
    value = metric.get(name)
    reason = metric_reason(metric, name)
    if value is None:
        return f"未算定（{reason}）" if reason else "未算定"
    if name == "profit_factor" and isinstance(value, str) and value.lower() in ("inf", "infinity"):
        return "∞（損失なし）"
    return ratio(value)


def chart_points(plan: dict, metric: dict) -> tuple[list[dict], str]:
    curve = list(plan.get("realized_curve") or [])
    curve = [
        row for row in curve if iso_day(row.get("date")) and number(row.get("equity")) is not None
    ]
    curve.sort(key=lambda row: row["date"])
    baseline = str(first(metric, "baseline_date", "effective_start", default=""))
    end = str(first(metric, "end", default="9999-12-31"))
    selected = [row for row in curve if baseline <= row["date"] <= end]
    if selected:
        return selected, baseline or selected[0]["date"]
    return [], baseline


def make_chart(key: str, plan: dict, metric: dict, width: float, height: float) -> Drawing:
    """Draw realized funds plus a same-baseline TOPIX equivalent with vector paths."""
    drawing = Drawing(width, height)
    drawing.add(
        Rect(0, 0, width, height, fillColor=colors.white, strokeColor=RULE, strokeWidth=0.5)
    )
    drawing.add(
        String(
            13,
            height - 19,
            "運用開始からの確定損益ベース資金推移",
            fontName=BOLD,
            fontSize=11,
            fillColor=INK,
        )
    )
    rows, baseline = chart_points(plan, metric)
    if not rows:
        drawing.add(
            String(
                24,
                height / 2,
                "期間内の曲線データなし",
                fontName=FONT,
                fontSize=10,
                fillColor=MUTED,
            )
        )
        return drawing
    base_equity = number(rows[0].get("equity"))
    base_topix = number(rows[0].get("topix_close"))
    base_topix_equity = number(rows[0].get("topix_equity"))
    benchmark = []
    for row in rows:
        close = number(row.get("topix_close"))
        indexed = number(row.get("topix_equity"))
        if base_equity is not None and base_topix and close is not None:
            benchmark.append(base_equity * close / base_topix)
        elif base_equity is not None and base_topix_equity and indexed is not None:
            benchmark.append(base_equity * indexed / base_topix_equity)
        else:
            benchmark.append(None)
    equity = [number(row.get("equity")) for row in rows]
    values = [v for v in equity + benchmark if v is not None]
    low, high = min(values), max(values)
    span = max(high - low, abs(base_equity or 0) * 0.002, 100)
    bottom, top = low - span * 0.18, high + span * 0.18
    left, right = 80, width - 128
    lower, upper = 42, height - 50
    first_date, last_date = iso_day(rows[0]["date"]), iso_day(rows[-1]["date"])
    days = max((last_date - first_date).days, 1)

    def xpos(index: int) -> float:
        if first_date == last_date:
            return left + (right - left) / 2
        return left + (iso_day(rows[index]["date"]) - first_date).days / days * (right - left)

    def ypos(amount: float) -> float:
        return lower + (amount - bottom) / (top - bottom) * (upper - lower)

    for index in range(4):
        amount = bottom + (top - bottom) * index / 3
        y = ypos(amount)
        drawing.add(Line(left, y, right, y, strokeColor=RULE, strokeWidth=0.5))
        drawing.add(
            String(
                left - 9,
                y - 3,
                f"{amount / 10000:,.1f}万円",
                fontName=FONT,
                fontSize=9,
                textAnchor="end",
                fillColor=MUTED,
            )
        )
    drawing.add(Line(left, lower, left, upper, strokeColor=RULE, strokeWidth=0.5))
    label_indices = sorted({0, len(rows) // 2, len(rows) - 1})
    for index in label_indices:
        label = str(rows[index]["date"])[5:]
        drawing.add(
            String(
                xpos(index),
                lower - 16,
                label,
                fontName=FONT,
                fontSize=9,
                textAnchor="middle",
                fillColor=MUTED,
            )
        )
    last_equity = equity[-1]
    plan_color = RED if last_equity > base_equity else BLUE if last_equity < base_equity else INK
    for series, color, dashed in ((benchmark, TOPIX, True), (equity, plan_color, False)):
        segment = []
        for index, amount in enumerate(series):
            if amount is not None:
                segment.extend([xpos(index), ypos(amount)])
            elif segment:
                if len(segment) >= 4:
                    drawing.add(
                        PolyLine(
                            segment,
                            strokeColor=color,
                            strokeWidth=1.7,
                            strokeDashArray=[4, 3] if dashed else None,
                        )
                    )
                segment = []
        if len(segment) >= 4:
            drawing.add(
                PolyLine(
                    segment,
                    strokeColor=color,
                    strokeWidth=1.7,
                    strokeDashArray=[4, 3] if dashed else None,
                )
            )
        for index in {0, len(series) - 1}:
            if series[index] is not None:
                drawing.add(
                    Circle(
                        xpos(index),
                        ypos(series[index]),
                        2.5,
                        fillColor=color,
                        strokeColor=colors.white,
                        strokeWidth=0.7,
                    )
                )
    # Endpoint labels are staggered when lines meet, rather than drawn on top of each other.
    last_benchmark = benchmark[-1]
    labels = [(f"プラン{key}", last_equity, plan_color)]
    if last_benchmark is not None:
        labels.append(("TOPIX換算", last_benchmark, TOPIX))
    label_positions = [ypos(amount) for _, amount, _ in labels]
    if len(labels) == 2 and abs(label_positions[0] - label_positions[1]) < 29:
        midpoint = sum(label_positions) / 2
        label_positions = [midpoint + 16, midpoint - 16]
    for (label, amount, color), y in zip(labels, label_positions, strict=True):
        y = max(lower + 4, min(upper - 7, y))
        drawing.add(
            Line(
                xpos(len(rows) - 1) + 4,
                ypos(amount),
                right + 8,
                y,
                strokeColor=color,
                strokeWidth=0.7,
            )
        )
        drawing.add(String(right + 13, y + 3, label, fontName=FONT, fontSize=9, fillColor=color))
        drawing.add(
            String(
                right + 13, y - 9, f"{amount:,.2f} 円", fontName=FONT, fontSize=9, fillColor=color
            )
        )
    drawing.add(
        String(
            13,
            12,
            f"比較基準 {baseline}  |  TOPIXは同日の資金額へ換算  |  保有評価損益を含まない",
            fontName=FONT,
            fontSize=9,
            fillColor=MUTED,
        )
    )
    return drawing


def overview(packet: dict, period: str) -> list:
    start, end = period_bounds(packet, period)
    story = [p(f"フォワード運用 {PERIOD_LABELS[period]}レポート", "title")]
    term = f"{start} - {end}"
    if period == "week":
        term += "  |  月曜起点・日曜区切り／週の途中経過"
    elif period == "year":
        term += "  |  年初来の途中経過"
    story += [p(term, "subtitle"), Spacer(1, 5)]
    story += [p("全5プランの現在地", "section"), Spacer(1, 5)]
    rows = [
        [
            p(h, "head")
            for h in (
                "プラン",
                "確定資金（円）",
                "累積確定損益（円）",
                "保有評価損益（円）",
                "保有数",
            )
        ]
    ]
    for key in PLAN_ORDER:
        plan = packet.get("plans", {}).get(key, {})
        inception = metrics(plan, "inception")
        meta = plan_meta(plan)
        initial = first(
            plan,
            "capital",
            "initial_capital",
            "initial_cash",
            default=first(meta, "capital", "initial_capital", "initial_cash"),
        )
        realized = first(inception, "closed_profit_jpy", "realized_pnl")
        equity = first(plan, "realized_equity", "realized_funds")
        if equity is None and plan.get("realized_curve"):
            equity = plan["realized_curve"][-1].get("equity")
        if equity is None and number(initial) is not None and number(realized) is not None:
            equity = number(initial) + number(realized)
        rows.append(
            [
                p(f"プラン{key}"),
                p(money(equity), "right"),
                signed_cell(realized, lambda x: money(x, True)),
                signed_cell(plan.get("unrealized_jpy"), lambda x: money(x, True)),
                p(
                    len(plan["holdings"]) if isinstance(plan.get("holdings"), list) else "未確認",
                    "right",
                ),
            ]
        )
    story += [compact_table(rows, [150, 160, 175, 185, CONTENT_WIDTH - 670]), Spacer(1, 6)]
    for metric_period, title in ((period, "対象期間の比較"), ("inception", "各プラン開始来の比較")):
        rows = [
            [
                p(h, "cell")
                for h in (
                    "プラン",
                    "確定収益率",
                    "TOPIX",
                    "超過差",
                    "Sharpe",
                    "PF",
                    "最大DD",
                    "決済数",
                )
            ]
        ]
        for key in PLAN_ORDER:
            metric = metrics(packet.get("plans", {}).get(key, {}), metric_period)
            count = first(
                metric,
                "closed_trade_count",
                "trade_count",
                "closed_count",
                default=(metric.get("counts") or {}).get("closed"),
            )
            rows.append(
                [
                    p(key, "head"),
                    signed_cell(metric.get("return_pct"), percent),
                    signed_cell(metric.get("topix_return_pct"), percent),
                    signed_cell(metric.get("excess_points"), lambda x: percent(x, "pt")),
                    p(ratio(metric.get("sharpe")), "right"),
                    p(ratio(metric.get("profit_factor")), "right"),
                    signed_cell(metric.get("max_drawdown_pct"), percent),
                    p("未確認" if count is None else count, "right"),
                ]
            )
        story += [
            p(title, "section"),
            Spacer(1, 3),
            compact_table(rows, [50, 101, 91, 91, 143, 132, 95, CONTENT_WIDTH - 703]),
        ]
    story += [
        Spacer(1, 4),
        p(
            "確定資金 = 初期資金 + 決済済み取引の費用控除後損益。保"
            "有株の値動きは含めず、評価損益を別掲。赤"
            "は利益・プラス、青は損失・マイナス。開始日の違う口座は各口座"
            "の実際の観測期間で比較しています。",
            "small",
        ),
    ]
    return story


def metric_panel(plan: dict, period: str) -> list:
    period_metric, inception = metrics(plan, period), metrics(plan, "inception")
    rows = [
        [
            p("指標", "head"),
            p("対象期間", "head"),
            p("開始来", "head"),
            p("指標", "head"),
            p("対象期間", "head"),
            p("開始来", "head"),
        ]
    ]
    pairs = [
        (
            ("確定損益（円）", "closed_profit_jpy", lambda x: money(x, True)),
            ("収益率", "return_pct", percent),
        ),
        (
            ("TOPIX騰落率", "topix_return_pct", percent),
            ("TOPIX超過差", "excess_points", lambda x: percent(x, "pt")),
        ),
        (("Sharpe", "sharpe", None), ("PF", "profit_factor", None)),
        (("最大DD", "max_drawdown_pct", percent), ("支払費用（円）", "fees", money)),
    ]
    for left, right in pairs:
        row = []
        for label, key, formatter in (left, right):
            row.append(p(label))
            for metric in (period_metric, inception):
                if formatter is None:
                    row.append(p(ratio_metric(metric, key), "right"))
                elif key == "fees":
                    row.append(p(formatter(first(metric, "fees", "fees_jpy")), "right"))
                else:
                    row.append(signed_cell(metric.get(key), formatter))
        rows.append(row)
    counts = [str(m.get("daily_observations", "未確認")) for m in (period_metric, inception)]
    trades = [str(m.get("trade_count", "未確認")) for m in (period_metric, inception)]
    return [
        table(rows, [112, 139, 138, 112, 139, CONTENT_WIDTH - 640]),
        Spacer(1, 3),
        p(
            f"日次観測数: 期間 {counts[0]} / 開始来 {counts[1]}  |  "
            f"決済数: 期間 {trades[0]} / 開始来 {trades[1]}  |  "
            "支払費用は保有分の買付費用も含む",
            "small",
        ),
    ]


def holding_table(holdings: list[dict] | None) -> list:
    if holdings is None:
        return [p("保有情報は未確認", "small")]
    if not holdings:
        return [p("保有なし", "small")]
    headers = (
        "銘柄",
        "株数",
        "買付日",
        "買付単価",
        "現在終値 / 価格日",
        "評価損益（円）",
        "評価損益率",
    )
    rows = [[p(h, "cell") for h in headers]]
    for item in holdings:
        rows.append(
            [
                p(f"{item.get('code', '')} {item.get('name') or '銘柄名未確認'}"),
                p(money(item.get("shares"), decimals=0), "right"),
                p(item.get("entry_date")),
                p(money(item.get("entry_price")), "right"),
                p(
                    f"{money(item.get('current_price'))} / "
                    f"{item.get('price_date') or '価格日未確認'}",
                    "right",
                ),
                signed_cell(item.get("unrealized_jpy"), lambda x: money(x, True)),
                signed_cell(item.get("unrealized_pct"), percent),
            ]
        )
    return [table(rows, [205, 50, 88, 92, 130, 120, CONTENT_WIDTH - 685])]


def buys_table(plan: dict, packet: dict) -> list:
    buys = plan.get("planned_buys")
    if buys is None or (
        not buys
        and plan.get("next_plan_status") in ("pending", "unavailable", "missing", "unconfirmed")
    ):
        return [p("次営業日の買付判断は未確認・未確定です。", "small")]
    if not buys:
        reason = first(
            plan,
            "no_buy_reason",
            "planned_buys_reason",
            "next_buy_reason",
            default="買付予定なし。判断の確定状況は運用状態を参照。",
        )
        reason = {
            "topix_below_or_equal_sma20": "TOPIXが20日移動平均以下のため買付予定なし",
        }.get(str(reason), reason)
        return [p(reason, "small")]
    headers = ("銘柄", "株数", "前日終値", "状態", "理由／条件")
    rows = [[p(h, "cell") for h in headers]]
    for item in buys:
        status = value_text(item.get("status"))
        status = STATUS_LABELS.get(status, status)
        reason = value_text(item.get("reason"))
        target = item.get("target")
        if target is not None and str(target) != str(packet.get("next_session")):
            reason += f"\n対象: {value_text(target)}"
        rows.append(
            [
                p(f"{item.get('code', '')} {item.get('name') or '銘柄名未確認'}"),
                p(money(item.get("shares"), decimals=0), "right"),
                p(money(item.get("prev_close")), "right"),
                p(status),
                p(reason),
            ]
        )
    return [table(rows, [190, 50, 90, 130, CONTENT_WIDTH - 460])]


def closes_table(plan: dict, metric: dict) -> list:
    if not isinstance(plan.get("closed_trades"), list):
        return [p("決済取引情報は未確認", "small")]
    start = str(first(metric, "effective_start", "start", default=""))
    end = str(first(metric, "end", default="9999-12-31"))
    trades = [
        item
        for item in plan.get("closed_trades", [])
        if start <= str(item.get("exit_date", ""))[:10] <= end
    ]
    if not trades:
        return [p("対象期間の決済なし", "small")]
    rows = [
        [
            p(h, "cell")
            for h in (
                "銘柄",
                "買付日／売却日",
                "株数",
                "買付単価",
                "売却単価",
                "費用（円）",
                "確定損益（円）",
            )
        ]
    ]
    for item in trades:
        rows.append(
            [
                p(f"{item.get('code', '')} {item.get('name') or '銘柄名未確認'}"),
                p(f"{item.get('entry_date') or '未確認'}\n{item.get('exit_date') or '未確認'}"),
                p(money(item.get("shares"), decimals=0), "right"),
                p(money(item.get("entry_price")), "right"),
                p(money(item.get("exit_price")), "right"),
                p(money(item.get("fees")), "right"),
                signed_cell(item.get("net"), lambda x: money(x, True)),
            ]
        )
    return [table(rows, [190, 110, 50, 100, 100, 100, CONTENT_WIDTH - 650])]


def plan_story(key: str, plan: dict, packet: dict, period: str, chart: Drawing) -> list:
    metric = metrics(plan, period)
    story = [p(plan_name(key, plan), "title")]
    meta = plan_meta(plan)
    start = first(
        metrics(plan, "inception"),
        "effective_start",
        default=first(
            plan,
            "start",
            "start_date",
            default=first(meta, "start", "start_date", "inception_date"),
        ),
    )
    effective = first(metric, "effective_start", "start")
    baseline = first(metric, "baseline_date", default="未確認")
    story += [
        p(
            f"開始日 {start or '未確認'}  |  "
            f"対象期間 {effective or '未確認'} - {metric.get('end') or packet['asof']}  |  "
            f"比較基準日 {baseline}",
            "subtitle",
        ),
        Spacer(1, 8),
    ]
    story += metric_panel(plan, period)
    story += [Spacer(1, 9), chart, Spacer(1, 7)]
    status = plan.get("status")
    issues = plan.get("issues") or []
    status_text = STATUS_LABELS.get(str(status), value_text(status)) if status else ""
    if status_text or issues:
        text = "運用状態: " + (status_text or "未確認")
        if issues:
            text += "\n確認事項: " + value_text(issues)
        if status != "complete" or issues:
            story += [p(text, "small"), Spacer(1, 3)]
    holdings = plan.get("holdings")
    holding_title = "保有銘柄" + (
        f"  |  {len(holdings)}銘柄" if isinstance(holdings, list) else "  |  未確認"
    )
    holding_section = [
        p(holding_title, "section"),
        Spacer(1, 4),
        *holding_table(holdings),
        Spacer(1, 3),
        p(
            "評価損益は買付費用を含み、将来の売却費用は含みません。現在終"
            "値と価格日は銘柄ごとに表示。",
            "small",
        ),
    ]
    story.append(KeepTogether(holding_section))
    next_session = packet.get("next_session") or "未確認"
    story += section(f"次営業日の買付候補  |  {next_session}", buys_table(plan, packet))
    story += section("対象期間の決済取引", closes_table(plan, metric))
    return story


def methodology_story(packet: dict) -> list:
    story = [p("指標の読み方と集計範囲", "title"), Spacer(1, 10)]
    entries = [
        (
            "確定資金・確定収益率",
            "確定資金は初期資金に決済済み取引の費用控除後損益を加えた金額"
            "です。買付に使った現金をそのまま減算する"
            "資金残高ではありません。収益率は対象期間の比較基準資金に対す"
            "る確定損益で算定し、含み損益を除きます。",
        ),
        (
            "TOPIX・超過差",
            "各プランの対象期間と比較基準日に合わせてTOPIXを比較しま"
            "す。チャートは同日の確定資金額を起点にT"
            "OPIXを換算。超過差は確定収益率 - TOPIX騰落率（パ"
            "ーセントポイント）です。",
        ),
        (
            "Sharpe",
            "確定資金の日次リターンに基づく年率換算指標（年252営業日、"
            "無リスク金利0）。観測数不足や日次変動が"
            "ゼロの場合は未算定と表示します。短い観測期間の値を安定した実"
            "力と解釈しないでください。",
        ),
        (
            "PF・最大DD",
            "PFは決済取引の利益合計 ÷ 損失絶対額合計。決済がない場合"
            "は未算定です。最大DDは確定資金の過去最"
            "高値からの最大下落率。保有株の値下がりを含まないため、保有中"
            "のリスクを過小表示します。",
        ),
        (
            "保有評価損益",
            "銘柄ごとの現在終値による時価から買付代金・買付費用を差し引き"
            "ます。将来の売却費用は未控除。未決済損益を確定損益・確定資金"
            "・確定曲線へ混ぜません。",
        ),
        (
            "期間と運用段階",
            "日次は当日、週次は月曜から日曜の週の当日時点、年次は1月1日"
            "から当日時点です。各プランの実際の開始前"
            "の実績は作りません。すべてペーパー運用記録で、実取引の約定実"
            "績ではありません。",
        ),
    ]
    rows = [[p(title, "head"), p(text)] for title, text in entries]
    story += [table(rows, [145, CONTENT_WIDTH - 145], header=False), Spacer(1, 5)]
    generated = packet.get("generated_at") or "未確認"
    digest = packet.get("dataset_sha256") or "未確認"
    manifest = packet.get("input_manifest") or []
    source_count = len(manifest)
    story += section(
        "資料の確認情報",
        [
            p(
                f"集計基準日: {packet['asof']}\n作成時刻: {generated}\n"
                f"入力資料数: {source_count}\n資料識別 SHA-256: {digest}",
                "small",
            )
        ],
    )
    return story


def render_report(packet: dict, period: str, output: Path) -> dict:
    asof = date.fromisoformat(packet["asof"])
    if period == "day":
        name = f"日次_{asof.isoformat()}.pdf"
    elif period == "week":
        week_year, week_number, _ = asof.isocalendar()
        name = f"週次_{week_year}-W{week_number:02d}_{asof.isoformat()}時点.pdf"
    else:
        name = f"年次_{asof.year}年_{asof.isoformat()}時点.pdf"
    path = output / name
    chart_dir = output / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    chart_paths = []
    story = overview(packet, period)
    for key in PLAN_ORDER:
        plan = packet.get("plans", {}).get(key, {})
        chart = make_chart(key, plan, metrics(plan, "inception"), CONTENT_WIDTH, 150)
        chart_path = chart_dir / f"{period}_{key}.svg"
        renderSVG.drawToFile(chart, str(chart_path))
        # The PDF embeds the fonts; standalone SVG refers to the installed face.
        svg = chart_path.read_text(encoding="utf-8")
        svg = svg.replace(f"font-family: {BOLD};", "font-family: Meiryo; font-weight: bold;")
        svg = svg.replace(f"font-family: {FONT};", "font-family: Meiryo;")
        chart_path.write_text(svg, encoding="utf-8")
        chart_paths.append(str(chart_path.resolve()))
        story += [PageBreak(), *plan_story(key, plan, packet, period, chart)]
    story += [PageBreak(), *methodology_story(packet)]
    doc = SimpleDocTemplate(
        str(path),
        pagesize=PAGE_SIZE,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=39,
        bottomMargin=34,
        title=f"フォワード運用 {PERIOD_LABELS[period]} {packet['asof']}",
        author="個別株自動売買ツールV1",
        pageCompression=1,
    )

    def footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, 26, PAGE_SIZE[0] - MARGIN, 26)
        canvas.setFont(FONT, 9)
        canvas.setFillColor(MUTED)
        canvas.drawString(
            MARGIN, 13, f"ペーパー運用  |  {PERIOD_LABELS[period]}  |  {packet['asof']}時点"
        )
        canvas.drawRightString(PAGE_SIZE[0] - MARGIN, 13, str(document.page))
        if document.page > 1:
            canvas.drawString(
                MARGIN, PAGE_SIZE[1] - 23, "FORWARD PERFORMANCE  /  確定損益と保有評価を分けて表示"
            )
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return {"path": str(path.resolve()), "pages": doc.page, "charts": chart_paths}


def render_packet(packet: dict, output: Path) -> dict:
    rl_config.invariant = True
    register_fonts()
    STYLES.update(styles())
    date.fromisoformat(packet["asof"])
    missing = [key for key in PLAN_ORDER if key not in packet.get("plans", {})]
    if missing:
        raise ValueError("対象5プランの入力が不足しています: " + ", ".join(missing))
    output.mkdir(parents=True, exist_ok=True)
    reports = {period: render_report(packet, period, output) for period in PERIOD_LABELS}
    return {"asof": packet["asof"], "reports": reports}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forward paper-account PDF packet renderer")
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    packet = json.loads(args.packet.read_text(encoding="utf-8-sig"))
    print(json.dumps(render_packet(packet, args.output), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
