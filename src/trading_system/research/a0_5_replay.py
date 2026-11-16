"""Recompute the three fixed A0_5 accounts from the original local, read-only snapshot."""

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import mkdtemp

from .a0_5_rules.backtester import backtest, save_result
from .a0_5_rules.config import A0_5

REVISION = "68dbce1d520d03d19107c5571da59d931bf1da33"
DIGEST = "c141825051581b8a8068449dbc682330197502d64e8215a8f87226df40e15bdb"
SNAPSHOT = "2017-01-04_2026-11-13_c14182505158.json"
PERIODS = {
    "full": ("2017-01-04", "2026-11-13"),
    "IS": ("2017-01-04", "2021-12-31"),
    "OOS": ("2022-01-01", "2026-11-13"),
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def compare(actual, expected):
    for name in ("equity.csv", "executions.csv", "orders.csv", "run.json"):
        if not (expected / name).is_file():
            return {"file": name, "reason": "missing_reference"}
        if name == "run.json":
            a, b = [json.loads((p / name).read_bytes()) for p in (actual, expected)]
            if a != b:
                return {
                    "file": name,
                    "difference": {
                        k: [a.get(k), b.get(k)] for k in a.keys() | b.keys() if a.get(k) != b.get(k)
                    },
                }
        else:
            # Normalize line endings only. Preserve code strings, ordering and every numeric digit.
            a, b = [(p / name).read_text("utf-8-sig").splitlines() for p in (actual, expected)]
            if a != b:
                from itertools import zip_longest

                i, pair = next(
                    (i, pair) for i, pair in enumerate(zip_longest(a, b), 1) if pair[0] != pair[1]
                )
                return {"file": name, "line": i, "columns": a[0], "actual_expected": pair}
    return None


def replay(source, output):
    path = source / "data/snapshots" / SNAPSHOT
    if digest(path) != DIGEST:
        raise ValueError("fixed snapshot SHA-256 mismatch")
    output.mkdir(parents=True, exist_ok=True)
    stage = Path(mkdtemp(prefix="replay-", dir=output))
    info = json.loads(path.read_bytes())
    parts = {
        part["file"]: digest(source / "data" / part["file"])
        for group in info["files"].values()
        for part in group
    }
    (stage / "inputs.json").write_text(json.dumps(parts, indent=2), encoding="utf-8")
    summary = {
        "mode": "固定履歴再現",
        "revision": REVISION,
        "snapshot_sha256": DIGEST,
        "periods": {},
    }
    print(stage, flush=True)
    for period, (start, end) in PERIODS.items():
        result = backtest(path, start, end, A0_5)
        saved = save_result(result, A0_5, path, start, end, stage)
        actual = stage / period
        saved.rename(actual)
        diff = compare(actual, source / "results/final-diversification-20261116/A0-5" / period)
        days = (result.equity.date.iloc[-1] - result.equity.date.iloc[0]).days
        summary["periods"][period] = {
            **result.metrics,
            "cagr": (1 + result.metrics["return"]) ** (365.25 / days) - 1,
            "first_difference": diff,
        }
        (stage / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            period,
            result.metrics["sessions"],
            result.metrics["return"],
            "MATCH" if not diff else diff,
            flush=True,
        )
    if any(p["first_difference"] for p in summary["periods"].values()):
        raise ValueError("fixed replay differs from reference")
    return stage


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/a0-5-replay"))
    args = parser.parse_args()
    replay(args.source.resolve(), args.output.resolve())
