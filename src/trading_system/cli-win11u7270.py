"""Local acquisition, synthetic simulation and model research; no brokerage calls."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from trading_system.coverage import audit_coverage
from trading_system.data_sources.jquants import APIError, JQuantsClient
from trading_system.ingestion.prices import incremental_start, ingest_prices, price_query
from trading_system.market_financial import ingest_market_financial, market_financial_query
from trading_system.reference import ingest_reference, reference_query
from trading_system.settings import Settings, api_key
from trading_system.storage.store import Store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Auditable data acquisition and research simulation"
    )
    parser.add_argument("--config", type=Path, default=Path("config/stage1.toml"))
    parser.add_argument("--data-root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check configuration without showing API credentials")
    sub.add_parser("status", help="Show acquisition and quality counts")
    sub.add_parser("export", help="Rebuild and verify Parquet from the DuckDB catalog")
    fetch = sub.add_parser("fetch-prices", help="Acquire one code for an explicit date interval")
    fetch.add_argument("--code", required=True)
    fetch.add_argument("--from", dest="start", required=True)
    fetch.add_argument("--to", dest="end", required=True)
    fetch.add_argument(
        "--refresh", action="store_true", help="New scan, including completed intervals"
    )
    fetch.add_argument(
        "--incremental", action="store_true", help="Overlap last completion by 7 days"
    )
    master = sub.add_parser("fetch-master", help="Acquire a dated listing snapshot")
    master.add_argument("--date", required=True)
    master.add_argument("--code")
    master.add_argument("--refresh", action="store_true")
    calendar = sub.add_parser("fetch-calendar", help="Acquire all calendar days in a range")
    calendar.add_argument("--from", dest="start", required=True)
    calendar.add_argument("--to", dest="end", required=True)
    calendar.add_argument("--refresh", action="store_true")
    topix = sub.add_parser("fetch-topix", help="Acquire TOPIX for an explicit date interval")
    topix.add_argument("--from", dest="start", required=True)
    topix.add_argument("--to", dest="end", required=True)
    topix.add_argument("--refresh", action="store_true")
    financials = sub.add_parser("fetch-financials", help="Acquire summary by code and/or date")
    financials.add_argument("--code")
    financials.add_argument("--date")
    financials.add_argument("--refresh", action="store_true")
    audit = sub.add_parser(
        "audit-coverage", help="Audit stored prices/TOPIX/calendar without API calls"
    )
    audit.add_argument("--code", required=True)
    audit.add_argument("--from", dest="start", required=True)
    audit.add_argument("--to", dest="end", required=True)
    audit.add_argument("--decision-at", help="Timezone-aware ISO 8601 timestamp (default: now)")
    backtest = sub.add_parser(
        "backtest-benchmark", help="Run deterministic synthetic research assumptions without APIs"
    )
    backtest.add_argument("--scenario", type=Path, help="Explicit synthetic scenario JSON")
    backtest.add_argument("--output", type=Path, help="Save full validated input and result JSON")
    models = sub.add_parser(
        "model-benchmark", help="Run frozen synthetic model research without real-market fitting"
    )
    models.add_argument("--scenario", type=Path, help="Action-free synthetic scenario JSON")
    models.add_argument("--research-config", type=Path, help="Chronological research configuration")
    models.add_argument("--registry-root", type=Path, default=Path("data/research"))
    models.add_argument("--output", type=Path, help="Save full research/model artifact JSON")
    portfolio = sub.add_parser(
        "portfolio-benchmark", help="Plan fixed synthetic allocations and risk without execution"
    )
    portfolio.add_argument("--plan", type=Path, help="Explicit dated synthetic planning input JSON")
    portfolio.add_argument(
        "--output", type=Path, help="Save full input, proposals and risk audit JSON"
    )
    simulation = sub.add_parser(
        "portfolio-simulation",
        help="Replay fixed synthetic portfolios and isolated strategy budgets",
    )
    simulation.add_argument(
        "--scenario", type=Path, help="Explicit synthetic portfolio replay JSON"
    )
    simulation.add_argument(
        "--output", type=Path, help="Save full replay and fixed-budget comparison"
    )
    execution = sub.add_parser(
        "execution-mock", help="Exercise durable offline MOCK order recovery"
    )
    execution.add_argument("--state-root", type=Path, default=Path("data/execution-mock"))
    execution.add_argument("--output", type=Path)
    monitor = sub.add_parser(
        "monitor-mock", help="Read existing MOCK state without sending or recovering orders"
    )
    monitor.add_argument(
        "--database", type=Path, default=Path("data/execution-mock/manager.sqlite")
    )
    monitor.add_argument("--observed-at", help="Aware ISO timestamp; omit to use current UTC time")
    monitor.add_argument("--output", type=Path, help="JSON report outside the state directory")
    prices_monitor = sub.add_parser(
        "monitor-prices", help="Inspect one saved price acquisition query without API calls"
    )
    prices_monitor.add_argument(
        "--database", type=Path, default=Path("data/jquants/catalog.duckdb")
    )
    prices_monitor.add_argument("--code", required=True)
    prices_monitor.add_argument("--from", dest="start", required=True)
    prices_monitor.add_argument("--to", dest="end", required=True)
    prices_monitor.add_argument(
        "--max-age-hours",
        type=float,
        required=True,
        help="Explicit acquisition-age threshold; not a provider update guarantee",
    )
    prices_monitor.add_argument("--observed-at", help="Aware ISO timestamp; default current UTC")
    prices_monitor.add_argument("--output", type=Path, help="JSON outside the catalog directory")
    research_monitor = sub.add_parser(
        "monitor-research", help="Inspect saved synthetic evaluations without training or inference"
    )
    research_monitor.add_argument(
        "--result", type=Path, default=Path("data/research/model-result.json")
    )
    research_monitor.add_argument("--max-mse-ratio", type=float, required=True)
    research_monitor.add_argument("--max-mean-shift", type=float, required=True)
    research_monitor.add_argument("--minimum-rows", type=int, default=20)
    research_monitor.add_argument("--observed-at", help="Aware ISO timestamp; default current UTC")
    research_monitor.add_argument("--output", type=Path, help="JSON outside the research directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "monitor-research":
            from trading_system.monitoring.research import monitor_research
            from trading_system.storage.store import atomic_write, canonical

            if args.output and (
                args.output.suffix.lower() != ".json"
                or args.output.resolve().is_relative_to(args.result.resolve().parent)
            ):
                raise ValueError("Monitor output must be JSON outside the research directory")

            def strict_object(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("Duplicate JSON key")
                    value[key] = item
                return value

            def reject_constant(_):
                raise ValueError("Nonfinite JSON constant")

            now = (
                datetime.fromisoformat(args.observed_at) if args.observed_at else datetime.now(UTC)
            )
            source = json.loads(
                args.result.read_text(encoding="utf-8-sig"),
                object_pairs_hook=strict_object,
                parse_constant=reject_constant,
            )
            result = monitor_research(
                source,
                now=now,
                max_mse_ratio=args.max_mse_ratio,
                max_mean_shift=args.max_mean_shift,
                minimum_rows=args.minimum_rows,
            )
            if args.output:
                atomic_write(args.output, canonical(result).encode())
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "observed" else 3
        if args.command == "monitor-prices":
            from trading_system.monitoring.prices import monitor_prices
            from trading_system.storage.store import atomic_write, canonical

            if args.output and (
                args.output.suffix.lower() != ".json"
                or args.output.resolve().is_relative_to(args.database.resolve().parent)
            ):
                raise ValueError("Monitor output must be JSON outside the catalog directory")
            result = monitor_prices(
                args.database,
                code=args.code,
                start=args.start,
                end=args.end,
                now=datetime.fromisoformat(args.observed_at)
                if args.observed_at
                else datetime.now(UTC),
                max_age_hours=args.max_age_hours,
            )
            if args.output:
                atomic_write(args.output, canonical(result).encode())
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "observed" else 3
        if args.command == "monitor-mock":
            from trading_system.monitoring.execution import monitor_execution
            from trading_system.storage.store import atomic_write, canonical

            if args.output and (
                args.output.suffix.lower() != ".json"
                or args.output.resolve().is_relative_to(args.database.resolve().parent)
            ):
                raise ValueError("Monitor output must be JSON outside the state directory")
            now = (
                datetime.fromisoformat(args.observed_at) if args.observed_at else datetime.now(UTC)
            )
            result = monitor_execution(args.database, now)
            if args.output:
                atomic_write(args.output, canonical(result).encode())
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "observed" else 3
        if args.command == "execution-mock":
            from trading_system.execution.demo import run_mock_demo
            from trading_system.storage.store import atomic_write, canonical

            result = run_mock_demo(args.state_root)
            if args.output:
                atomic_write(args.output, canonical(result).encode())
            print(
                json.dumps(
                    {
                        k: result[k]
                        for k in (
                            "status",
                            "mode",
                            "real_execution_enabled",
                            "account_matches",
                            "halt",
                            "result_hash",
                        )
                    },
                    indent=2,
                )
            )
            return 0 if result["status"] == "complete" else 3
        if args.command == "portfolio-simulation":
            from trading_system.portfolio.comparison import compare_portfolio_strategies
            from trading_system.portfolio.simulation_benchmark import synthetic_simulation
            from trading_system.storage.store import atomic_write, canonical

            source = (
                json.loads(args.scenario.read_text(encoding="utf-8"))
                if args.scenario
                else synthetic_simulation()
            )
            result = compare_portfolio_strategies(source)
            if args.output:
                atomic_write(args.output, canonical(result).encode())
            print(
                json.dumps(
                    {
                        key: result[key]
                        for key in (
                            "status",
                            "mode",
                            "comparison_complete",
                            "execution_enabled",
                            "input_hash",
                            "result_hash",
                            "blocked_arms",
                        )
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if result["comparison_complete"] else 3
        if args.command == "portfolio-benchmark":
            from trading_system.portfolio.benchmark import synthetic_plan
            from trading_system.portfolio.planner import plan_portfolio
            from trading_system.storage.store import atomic_write, canonical

            request = (
                json.loads(args.plan.read_text(encoding="utf-8")) if args.plan else synthetic_plan()
            )
            result = plan_portfolio(request)
            if args.output:
                atomic_write(args.output, canonical(result).encode())
            print(
                json.dumps(
                    {
                        key: result[key]
                        for key in (
                            "status",
                            "mode",
                            "execution_enabled",
                            "input_hash",
                            "result_hash",
                            "blocked_reasons",
                        )
                    }
                    | {
                        "proposal_count": len(result["proposals"]),
                        "rejection_count": len(result["rejections"]),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if result["status"] == "complete" else 3
        if args.command == "model-benchmark":
            from trading_system.research.benchmark import model_scenario
            from trading_system.research.pipeline import run_research
            from trading_system.storage.store import atomic_write, canonical

            scenario = (
                json.loads(args.scenario.read_text(encoding="utf-8"))
                if args.scenario
                else model_scenario()
            )
            config = (
                json.loads(args.research_config.read_text(encoding="utf-8"))
                if args.research_config
                else {}
            )
            result = run_research(scenario, config, args.registry_root)
            if args.output:
                atomic_write(args.output, canonical(result).encode())
            summary = {
                key: result[key]
                for key in ("status", "mode", "experiment_id", "input_hash", "result_hash")
            }
            summary["cost_evaluation_complete"] = result.get("cost_evaluation_complete", False)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return (
                0 if result["status"] == "complete" and summary["cost_evaluation_complete"] else 3
            )
        if args.command == "backtest-benchmark":
            from trading_system.backtest.benchmark import synthetic_scenario
            from trading_system.backtest.engine import run_backtest
            from trading_system.storage.store import atomic_write, canonical

            scenario = (
                json.loads(args.scenario.read_text(encoding="utf-8"))
                if args.scenario
                else synthetic_scenario()
            )
            result = run_backtest(scenario)
            if args.output:
                atomic_write(args.output, canonical(result).encode())
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "complete" else 3
        settings = Settings.load(args.config)
        if args.data_root:
            settings = settings.model_copy(update={"data_root": args.data_root})
        if args.command == "audit-coverage":
            result = audit_coverage(
                settings.data_root,
                args.code,
                args.start,
                args.end,
                datetime.fromisoformat(args.decision_at) if args.decision_at else None,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["state"] == "clear" else 3
        if args.command == "doctor":
            print(
                json.dumps(
                    {
                        "stage": 1,
                        "plan": settings.plan,
                        "plan_verification": settings.plan_evidence,
                        "api_key_present": api_key() is not None,
                        "data_root": str(settings.data_root.resolve()),
                        "live_trading": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command in {"fetch-master", "fetch-calendar", "fetch-topix", "fetch-financials"}:
            key = api_key()
            if key is None:
                print("JQUANTS_API_KEY is not configured. No API call was made.")
                return 2
            dataset = args.command.removeprefix("fetch-")
            if dataset in {"master", "financials"}:
                query = reference_query if dataset == "master" else market_financial_query
                params = query(dataset, date=args.date, code=args.code)
            else:
                query = reference_query if dataset == "calendar" else market_financial_query
                params = query(dataset, start=args.start, end=args.end)
            with JQuantsClient(
                key.get_secret_value(), plan=settings.plan, endpoint=dataset
            ) as client:
                acquire = (
                    ingest_reference
                    if dataset in {"master", "calendar"}
                    else ingest_market_financial
                )
                result = acquire(
                    settings.data_root, client, dataset, params, settings.plan, args.refresh
                )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 3 if result.get("state") in {"quality_failed", "empty"} else 0
        if args.command == "fetch-prices":
            key = api_key()
            if key is None:
                print("JQUANTS_API_KEY is not configured. No API call was made.")
                return 2
            params = price_query(args.code, args.start, args.end)
        with Store(settings.data_root) as store:
            if args.command == "status":
                result = store.summary()
            elif args.command == "export":
                result = store.export()
            else:
                if args.incremental:
                    params["from"] = incremental_start(store, args.code, args.start, args.end)
                with JQuantsClient(key.get_secret_value(), plan=settings.plan) as client:
                    result = ingest_prices(store, client, params, settings.plan, args.refresh)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 3 if result.get("state") in {"quality_failed", "empty"} else 0
    except APIError as exc:
        print(json.dumps({"error": exc.category, "http_status": exc.status_code}))
        return 2
    except Exception as exc:
        # Never print exception bodies: HTTP libraries and validators may embed credentials.
        print(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "message": "Operation failed. Check configuration and acquisition status.",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
