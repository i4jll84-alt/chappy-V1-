"""Local screener, daily publication, paper reconciliation and historical research."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from .inputs import settings, validate
from .paper import settle
from .prepare import JST, build, daily, load, root, write
from .report import markdown, summarize
from .study import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["history", "backtest", "daily", "preview", "paper", "status"]
    )
    parser.add_argument(
        "--date", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--history", default="history-v1")
    parser.add_argument("--result", default="results-v1")
    parser.add_argument("--revision", action="store_true")
    args = parser.parse_args()
    p = Path.cwd()
    home = root(p)
    cfg = settings(p)
    if args.config:
        if args.action not in ("history", "backtest", "preview"):
            parser.error("custom conditions are research-only")
        cfg = validate(load(args.config))
    for name in (args.history, args.result):
        if Path(name).name != name or name in (".", "..", "daily", "paper", "previews"):
            parser.error("choose a separate research folder name")
    day = args.date or datetime.now(JST).date().isoformat()
    if args.action == "history":
        build(p, home / args.history, args.date or cfg["study_end"], cfg)
        return
    if args.action == "backtest":
        archived = load(home / args.history / "plan.json")["config"]
        if any(
            cfg[k] != archived[k]
            for k in (
                "liquidity",
                "liquidity_window",
                "liquidity_tests",
                "cap_yen",
                "fee",
                "rate",
                "max_volume_cv",
                "display",
                "study_start",
                "earliest_archive",
                "version",
            )
        ):
            parser.error("ranking settings require their own history")
        out = home / args.result
        run(p, home / args.history, out, cfg)
        metrics = summarize(home / args.history, out)
        markdown(metrics, cfg, out)
        write(home / "latest-result.json", dict(folder=out.name))
        return
    if args.action == "status":
        entries = sorted((home / "daily").glob("*/first.json"))
        result = load(entries[-1]) if entries else {"state": "awaiting_first_frozen_selection"}
    elif args.action == "paper":
        result = settle(p, day)
    else:
        result = daily(p, day, revision=args.revision, preview=args.action == "preview", cfg=cfg)
    brief = {k: v for k, v in result.items() if k != "candidates"}
    if "candidates" in result:
        brief["counts"] = {k: len(result["candidates"][k]) for k in "ABCD"}
    print(json.dumps(brief, ensure_ascii=False))


if __name__ == "__main__":
    main()
