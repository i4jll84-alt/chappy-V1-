"""Explicit local commands; no background service, brokerage calls, or model training."""

import argparse
import json
from pathlib import Path

from trading_system.data_sources.jquants import APIError, JQuantsClient
from trading_system.ingestion.prices import incremental_start, ingest_prices, price_query
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
    args = parser.parse_args(argv)
    try:
        settings = Settings.load(args.config)
        if args.data_root:
            settings = settings.model_copy(update={"data_root": args.data_root})
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
