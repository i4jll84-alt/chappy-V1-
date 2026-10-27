"""Explicit local commands; no background service, brokerage calls, or model training."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from trading_system.cli_offline import register as register_offline
from trading_system.cli_offline import run as run_offline
from trading_system.data_sources.jquants import APIError, JQuantsClient
from trading_system.ingestion.prices import incremental_start, ingest_prices, price_query
from trading_system.market_financial import ingest_market_financial, market_financial_query
from trading_system.reference import ingest_reference, reference_query
from trading_system.settings import Settings, api_key
from trading_system.storage.store import Store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="J-Quants Stage 1: data acquisition only")
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
    mock = sub.add_parser("execution-mock", help="Local synthetic order-state verification only")
    mock.add_argument("--state-root", type=Path, required=True)
    mock.add_argument("--output", type=Path, required=True)
    register_offline(sub)
    topix = sub.add_parser("fetch-topix", help="Existing TOPIX data acquisition")
    topix.add_argument("--from", dest="start", required=True)
    topix.add_argument("--to", dest="end", required=True)
    topix.add_argument("--refresh", action="store_true")
    financials = sub.add_parser("fetch-financials", help="Existing financial summaries")
    financials.add_argument("--code")
    financials.add_argument("--date")
    financials.add_argument("--refresh", action="store_true")
    coverage = sub.add_parser("audit-coverage", help="Inspect saved data coverage")
    coverage.add_argument("--code", required=True)
    coverage.add_argument("--from", dest="start", required=True)
    coverage.add_argument("--to", dest="end", required=True)
    coverage.add_argument("--decision-at")
    args = parser.parse_args(argv)
    try:
        offline = run_offline(args)
        if offline is not None:
            _, summary, exit_code = offline
            print(json.dumps(summary, ensure_ascii=False, default=str))
            return exit_code
        if args.command == "execution-mock":
            from trading_system.execution.demo import run_mock_demo
            from trading_system.storage.store import atomic_write, canonical

            result = run_mock_demo(args.state_root)
            atomic_write(args.output, canonical(result).encode("utf-8"))
            print(json.dumps({"status": result["status"], "real_execution_enabled": False}))
            return 0 if result["status"] == "complete" else 3
        settings = Settings.load(args.config)
        if args.data_root:
            settings = settings.model_copy(update={"data_root": args.data_root})
        if args.command == "audit-coverage":
            from trading_system.coverage import audit_coverage

            result = audit_coverage(
                settings.data_root,
                args.code,
                args.start,
                args.end,
                datetime.fromisoformat(args.decision_at) if args.decision_at else None,
            )
            print(json.dumps(result, ensure_ascii=False, default=str))
            return 0 if result["state"] == "clear" else 3
        if args.command in {"fetch-topix", "fetch-financials"}:
            dataset = args.command.removeprefix("fetch-")
            params = (
                market_financial_query(dataset, start=args.start, end=args.end)
                if dataset == "topix"
                else market_financial_query(dataset, code=args.code, date=args.date)
            )
            key = api_key()
            if key is None:
                print("JQUANTS_API_KEY is not configured. No API call was made.")
                return 2
            with JQuantsClient(
                key.get_secret_value(), plan=settings.plan, endpoint=dataset
            ) as client:
                result = ingest_market_financial(
                    settings.data_root, client, dataset, params, settings.plan, args.refresh
                )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 3 if result.get("state") in {"quality_failed", "empty"} else 0
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
        if args.command in {"fetch-master", "fetch-calendar"}:
            key = api_key()
            if key is None:
                print("JQUANTS_API_KEY is not configured. No API call was made.")
                return 2
            dataset = args.command.removeprefix("fetch-")
            params = (
                reference_query(dataset, date=args.date, code=args.code)
                if dataset == "master"
                else reference_query(dataset, start=args.start, end=args.end)
            )
            with JQuantsClient(
                key.get_secret_value(), plan=settings.plan, endpoint=dataset
            ) as client:
                result = ingest_reference(
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
