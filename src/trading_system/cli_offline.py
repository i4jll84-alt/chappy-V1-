"""Restore CLI routes to existing offline verification modules; no broker transport."""

import json
from datetime import UTC, datetime
from pathlib import Path

from trading_system.point_in_time.policy import aware_utc
from trading_system.storage.store import atomic_write, canonical


def register(sub):
    for name, source in (
        ("backtest-benchmark", "scenario"),
        ("portfolio-benchmark", "plan"),
        ("portfolio-simulation", "scenario"),
        ("model-benchmark", "scenario"),
    ):
        p = sub.add_parser(name, help="Explicit synthetic offline verification")
        p.add_argument("--" + source, type=Path)
        p.add_argument("--output", type=Path)
        if name == "model-benchmark":
            p.add_argument("--research-config", type=Path)
            p.add_argument("--registry-root", type=Path, required=True)
    for name, source in (
        ("monitor-mock", "database"),
        ("monitor-prices", "database"),
        ("monitor-research", "result"),
    ):
        p = sub.add_parser(name, help="Read-only inspection of saved evidence")
        p.add_argument("--" + source, type=Path, required=True)
        p.add_argument("--observed-at")
        p.add_argument("--output", type=Path)
        if name == "monitor-prices":
            p.add_argument("--code", required=True)
            p.add_argument("--from", dest="start", required=True)
            p.add_argument("--to", dest="end", required=True)
            p.add_argument("--max-age-hours", type=float, required=True)
        if name == "monitor-research":
            p.add_argument("--max-mse-ratio", type=float, required=True)
            p.add_argument("--max-mean-shift", type=float, required=True)
            p.add_argument("--minimum-rows", type=int, default=20)


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("nonfinite_json_value")

    return json.loads(path.read_bytes(), object_pairs_hook=pairs, parse_constant=invalid_constant)


def clock(value):
    return aware_utc(datetime.fromisoformat(value)) if value else datetime.now(UTC)


def protect(output, sources, *, whole_directory=False):
    if output is None:
        return
    target = output.resolve()
    for source in sources:
        if source is None:
            continue
        original = source.resolve()
        if target == original or (whole_directory and target.is_relative_to(original.parent)):
            raise ValueError("output_may_not_replace_source")


def run(args):
    """Return (saved result, printed summary, exit code), or None for data commands."""
    command = args.command
    if command not in {
        "backtest-benchmark",
        "portfolio-benchmark",
        "portfolio-simulation",
        "model-benchmark",
        "monitor-mock",
        "monitor-prices",
        "monitor-research",
    }:
        return None
    if command.startswith("monitor-"):
        source = args.result if command == "monitor-research" else args.database
        protect(args.output, [source], whole_directory=True)
        now = clock(args.observed_at)
        if command == "monitor-mock":
            from trading_system.monitoring.execution import monitor_execution

            result = monitor_execution(source, now)
        elif command == "monitor-prices":
            from trading_system.monitoring.prices import monitor_prices

            result = monitor_prices(
                source,
                code=args.code,
                start=args.start,
                end=args.end,
                now=now,
                max_age_hours=args.max_age_hours,
            )
        else:
            from trading_system.monitoring.research import monitor_research

            result = monitor_research(
                read_json(source),
                now=now,
                max_mse_ratio=args.max_mse_ratio,
                max_mean_shift=args.max_mean_shift,
                minimum_rows=args.minimum_rows,
            )
        code = 0 if result["status"] in {"observed", "clear"} else 3
    else:
        source = getattr(args, "plan", None) or getattr(args, "scenario", None)
        protect(args.output, [source, getattr(args, "research_config", None)])
        if command == "backtest-benchmark":
            from trading_system.backtest.benchmark import synthetic_scenario
            from trading_system.backtest.engine import run_backtest

            result = run_backtest(read_json(source) if source else synthetic_scenario())
        elif command == "portfolio-benchmark":
            from trading_system.portfolio.benchmark import synthetic_plan
            from trading_system.portfolio.planner import plan_portfolio

            result = plan_portfolio(read_json(source) if source else synthetic_plan())
        elif command == "portfolio-simulation":
            from trading_system.portfolio.comparison import compare_portfolio_strategies
            from trading_system.portfolio.simulation_benchmark import synthetic_simulation

            result = compare_portfolio_strategies(
                read_json(source) if source else synthetic_simulation()
            )
        else:
            from trading_system.research.benchmark import model_scenario
            from trading_system.research.pipeline import ResearchConfig, run_research

            if args.output and args.output.resolve().is_relative_to(args.registry_root.resolve()):
                raise ValueError("output_may_not_replace_registry")
            result = run_research(
                read_json(source) if source else model_scenario(),
                read_json(args.research_config) if args.research_config else ResearchConfig(),
                args.registry_root,
            )
        code = 0 if result["status"] == "complete" else 3
    summary = result
    if command == "portfolio-benchmark":
        summary = result | {"proposal_count": len(result["proposals"])}
    if args.output:
        atomic_write(args.output, canonical(result).encode())
    return result, summary, code
