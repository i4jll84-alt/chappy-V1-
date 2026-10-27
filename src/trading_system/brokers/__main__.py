"""Offline doctor and repeatable paper contract demonstration."""

import argparse
import json
from pathlib import Path

from trading_system.brokers.base import Unavailable
from trading_system.brokers.capabilities import capabilities
from trading_system.brokers.demo import fixture, run_demo
from trading_system.brokers.registry import BROKERS, load_config
from trading_system.brokers.router import BrokerRouter


def main(argv=None):
    parser = argparse.ArgumentParser(description="Phase 1 offline broker foundation")
    parser.add_argument("--config", type=Path, default=Path("config/brokers.toml"))
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor")
    demo = subs.add_parser("demo")
    demo.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args(argv)
    router = None
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            result = {
                "mode": "paper",
                "selected_broker": config.trading.broker,
                "real_execution_enabled": False,
                "network_connections_attempted": 0,
                "brokers": {
                    b: {
                        "enabled": config.brokers[b].enabled,
                        "live_verified": False,
                        "features": {f.value: c.model_dump() for f, c in capabilities(b).items()},
                    }
                    for b in BROKERS
                },
                "paper_features": {
                    f.value: c.model_dump() for f, c in capabilities("rakuten", paper=True).items()
                },
            }
        else:
            router = BrokerRouter(args.state_root, config, fixture())
            result = run_demo(router)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, Unavailable):
        # Do not echo arbitrary exception strings or configuration/credential content.
        print(json.dumps({"status": "blocked", "real_execution_enabled": False}))
        return 2
    finally:
        if router is not None:
            router.close()


if __name__ == "__main__":
    raise SystemExit(main())
