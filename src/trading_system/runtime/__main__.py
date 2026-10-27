"""Explicit, offline virtual-clock scenario runner; no background registration."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from trading_system.brokers.registry import Config, PaperFixture
from trading_system.brokers.router import BrokerRouter
from trading_system.runtime.handlers import Fixtures
from trading_system.runtime.models import Settings
from trading_system.runtime.service import Lifecycle


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stage A offline lifecycle scenario")
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--at", action="append", help="explicit virtual JST/UTC timestamp")
    args = parser.parse_args(argv)
    router = runtime = None
    try:
        data = json.loads(args.scenario.read_text(encoding="utf-8-sig"))
        settings = Settings.model_validate(data["settings"])
        fixture = PaperFixture.model_validate(data["paper_fixture"])
        responses = Fixtures.model_validate(data["responses"])
        ticks = [settings.now(datetime.fromisoformat(t)) for t in (args.at or data["ticks"])]
        if not ticks or ticks != sorted(ticks):
            raise ValueError("explicit_monotone_virtual_clock_required")
        current = [ticks[0]]
        router = BrokerRouter(args.state_root / "paper", Config(), fixture)
        runtime = Lifecycle(
            args.state_root / "lifecycle", settings, router, responses, lambda: current[0]
        )
        results = []
        for now in ticks:
            current[0] = now
            results.append(runtime.tick())
            if results[-1]["normal_exit"] or results[-1]["save_error"]:
                break
        print(
            json.dumps(
                dict(evidence="simulated", real_execution_enabled=False, steps=results),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2 if results[-1]["save_error"] else 0
    except Exception as exc:
        print(
            json.dumps(
                dict(
                    status="blocked",
                    error_category=type(exc).__name__,
                    normal_exit=False,
                    real_execution_enabled=False,
                )
            )
        )
        return 2
    finally:
        if runtime is not None:
            runtime.close()
        if router is not None:
            router.close()


if __name__ == "__main__":
    raise SystemExit(main())
