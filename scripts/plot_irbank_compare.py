"""Create a static research chart from verified, unchanged comparison outputs."""

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    result = args.result.resolve()
    source = result / "daily-nav.csv"
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    manifest = json.loads((result / "manifest.json").read_bytes())
    if manifest["outputs"].get("daily-nav.csv") != digest:
        raise ValueError("daily_nav_hash_mismatch")
    report = json.loads((result / "report.json").read_bytes())
    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project / "data/research-python-libs"))
    os.environ.setdefault("MPLCONFIGDIR", str(result / "plot-cache"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    font = Path(os.environ["WINDIR"]) / "Fonts/YuGothM.ttc"
    font_manager.fontManager.addfont(str(font))
    plt.rcParams.update(
        {
            "font.family": font_manager.FontProperties(fname=font).get_name(),
            "axes.unicode_minus": False,
            "font.size": 11,
        }
    )
    with source.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    dates = [datetime.fromisoformat(r["date"]) for r in rows]
    colors = {"original": "#2563a5", "approx": "#d46b1f"}
    labels = {"original": "原条件", "approx": "近似条件"}
    fig, axes = plt.subplots(
        2, 2, figsize=(13, 8), sharex=True, gridspec_kw={"height_ratios": [1.4, 1]}
    )
    for col, suffix in enumerate(("", "_slip")):
        for base in ("original", "approx"):
            series = [float(r[base + suffix]) for r in rows]
            peak = series[0]
            drawdown = []
            for nav in series:
                peak = max(peak, nav)
                drawdown.append((nav / peak - 1) * 100)
            axes[0, col].plot(
                dates,
                [v / 10000 for v in series],
                label=labels[base],
                color=colors[base],
                linewidth=1.5,
            )
            axes[1, col].plot(dates, drawdown, color=colors[base], linewidth=1.2)
        axes[0, col].set_title("現行費用" if not suffix else "現行費用＋片道滑り0.10%")
        axes[0, col].legend(loc="upper left", frameon=False)
        axes[0, col].axhline(300, color="#888888", linewidth=0.7, linestyle="--")
        axes[1, col].axhline(0, color="#888888", linewidth=0.7)
        axes[1, col].xaxis.set_major_locator(mdates.MonthLocator(interval=6))
        axes[1, col].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        for ax in axes[:, col]:
            ax.grid(axis="y", alpha=0.2)
            ax.spines[["top", "right"]].set_visible(False)
    axes[0, 0].set_ylabel("費用控除後の純資産（万円）")
    axes[1, 0].set_ylabel("過去最高資産からの下落率（%）")
    fig.suptitle("プランA：原条件と近似条件の比較", fontsize=17, x=0.07, ha="left")
    fig.text(0.07, 0.91, "2023年11月1日引け〜2026年11月19日引け｜初期資金300万円｜T+2受渡")
    fig.text(
        0.07,
        0.025,
        "近似条件＝市場別残高を合算・後日更新値を使用・制限措置を空欄通過。公式データで条件差を比較。"
        "\n全銘柄のIRBANK再生や10年成績ではなく、過去時点で利用可能だったデータの再現も未確認。",
        fontsize=9,
        color="#444444",
    )
    fig.subplots_adjust(left=0.09, right=0.97, top=0.85, bottom=0.13, hspace=0.18, wspace=0.23)
    destination = result / "comparison.png"
    fig.savefig(destination, dpi=160, facecolor="white")
    plt.close(fig)
    (result / "chart-manifest.json").write_text(
        json.dumps(
            {
                "source_sha256": digest,
                "matplotlib": matplotlib.__version__,
                "image_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "comparison_input_sha256": report.get("input_sha256"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(str(destination))


if __name__ == "__main__":
    main()
