"""Offline binding of the verified V21 daily runner to V1's dated market receipts."""

import hashlib
import io
import json
import sys
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from functools import lru_cache
from io import StringIO
from pathlib import Path
from types import ModuleType

import pandas as pd

from .daytrade_universe import calendar_days, dated_inputs
from .swing_forward import signature

VERSION = "v21-opening-limit-atr-v1"
HASHES = {
    "plan_a_account": "c6a8d63585c91e7bfe7c3064b2ff53653fcc1b3bb823c88a27ef07fa86fa0395",
    "plan_a_jsf": "9f0979200ccbdcd2c4148d5530b17999b5609fa6135da923a695773b7f765f5e",
    "daily_runner": "31fbe4828e078dd90b3c28d3a0f59f4ee9fb7421950af5ad961213cc41e73115",
}
SEED_SHA = "9d1809c2153f02199456b39f186174ee4af334c411468d86e6570272553df77b"
PRICE_FIELDS = {
    "Date": "date",
    "O": "open",
    "H": "high",
    "L": "low",
    "C": "close",
    "Va": "turnover",
    "AdjFactor": "adj_factor",
}


def engine_root(project):
    return Path(project) / "data/plan-forward/engines/v21"


def verified(path, digest):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("v21_source_or_seed_hash_mismatch")
    return data


@lru_cache(maxsize=2)
def engine(project):
    root = engine_root(project)
    namespace = "_v1_plan_b_" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    pkg = ModuleType(namespace)
    pkg.__path__ = []
    sys.modules[namespace] = pkg
    for name, expected in HASHES.items():
        path = root / (name + ".py")
        source = verified(path, expected).decode("utf8")
        # Isolate the two sibling imports without adding a user-writable directory to sys.path.
        if name == "daily_runner":
            for sibling in ("plan_a_account", "plan_a_jsf"):
                source = source.replace(
                    "from " + sibling + " import ", "from ." + sibling + " import "
                )
        key = namespace + "." + name
        module = ModuleType(key)
        module.__package__ = namespace
        module.__file__ = str(path)
        sys.modules[key] = module
        exec(compile(source, str(path), "exec"), module.__dict__)
    module.notify_discord = lambda message: None
    return module


def session_window(project, signal, now):
    day = date.fromisoformat(signal)
    root = project / "data/jquants"
    prior, _ = calendar_days(root, day - timedelta(days=26), now)
    current, _ = calendar_days(root, day, now)
    days = {**prior, **current}
    sessions = sorted(d for d, flag in days.items() if flag in ("1", "2"))
    index = sessions.index(signal)
    target = sessions[index + 1]
    window = sessions[max(0, index - 34) : index + 1]
    first = date.fromisoformat(window[0])
    if any(
        (first + timedelta(days=n)).isoformat() not in days
        for n in range((date.fromisoformat(target) - first).days + 1)
    ):
        raise ValueError("incomplete_exchange_calendar")
    return window, target


def market_inputs(project, window, now):
    root = project / "data/jquants"
    signal = window[-1]
    path = engine_root(project) / "seed.parquet"
    seed = pd.read_parquet(io.BytesIO(verified(path, SEED_SHA)))
    base = seed.loc[seed.date.isin(window) & (seed.date < signal) & (seed.date < "2026-11-27")]
    dates = [d for d in window if "2026-11-27" <= d <= signal]
    columns, master, refs = dated_inputs(root, dates, now)
    frame = pd.DataFrame(r for rows in columns.values() for r in rows)
    if frame.duplicated(["Date", "Code"]).any() or set(frame.Date) != set(dates):
        raise ValueError("v21_daily_coverage")
    frame["code5"] = frame.Code.astype(str)
    today = frame.loc[frame.Date == signal].copy()
    extra = frame.loc[frame.Date < signal, [*PRICE_FIELDS, "code5"]].rename(columns=PRICE_FIELDS)
    for k in ("open", "high", "low", "close", "adj_factor", "turnover"):
        extra[k] = pd.to_numeric(extra[k], errors="raise")
    history = pd.concat([base, extra], ignore_index=True) if not extra.empty else base.copy()
    counts = history.loc[history.close > 0].groupby("date").code5.nunique()
    expected = [d for d in window[:-1] if d >= "2026-10-27"]
    if len(expected) < 20 or any(counts.get(d, 0) < 500 for d in expected):
        raise ValueError("v21_incomplete_price_window")

    class SavedMarket:
        def fetch_daily_quotes(self, day):
            if day != signal:
                raise ValueError("v21_price_date")
            return today

        def fetch_listed_info(self, day):
            if day != signal:
                raise ValueError("v21_master_date")
            return pd.DataFrame(master.values()).assign(code5=lambda f: f.Code.astype(str))

    return history, SavedMarket(), refs


def credit_receipts(project, dates, asof):
    root = project / "data/swing-forward/credit"
    refs = []
    for day in dates:
        body = (root / day / "manifest.json").read_bytes()
        meta = json.loads(body)
        if meta["date"] != day or datetime.fromisoformat(meta["available_at"]) > asof:
            raise ValueError("v21_credit_after_cutoff")
        refs.append(
            dict(
                date=day, available_at=meta["available_at"], sha256=hashlib.sha256(body).hexdigest()
            )
        )
    return refs


def native_runner(project, scratch, capital, state, client=None, *, runner_type=None, **options):
    mod = engine(project)
    cfg = mod.LiveStrategyConfig(
        initial_capital=capital,
        data_dir=scratch / "data",
        jsf_verified_dir=scratch / "credit",
        state_path=scratch / "state.json",
        output_dir=scratch / "orders",
        corp_actions_path=project / "data/plan-forward/inputs/v21/corporate_actions.csv",
        **options,
    )
    runner_class = runner_type(mod.DailyTradingRunner) if runner_type else mod.DailyTradingRunner
    runner = runner_class(cfg, client)
    if state is not None:
        cfg.state_path.write_text(json.dumps(state, allow_nan=False), encoding="utf8")
    return runner


def initial_native(project, scratch, capital):
    runner = native_runner(project, scratch, capital, None)
    state = engine(project).PortfolioState.load(runner.cfg.state_path, capital)
    state.save(runner.cfg.state_path)
    return json.loads(runner.cfg.state_path.read_bytes())


def morning(project, scratch, previous, capital, window, target, now, cutoff, *, entries=None):
    history, client, refs = market_inputs(project, window, now)
    archive = project / "data/swing-forward/credit"
    try:
        jsf = credit_receipts(project, window[-2:], min(now, cutoff))
    except (FileNotFoundError, ValueError):
        if now < cutoff:
            raise
        archive = scratch / "unavailable_credit"
        jsf = []
    runner = native_runner(
        project,
        scratch,
        capital,
        previous,
        client,
        previous_credit_day=window[-2],
        jsf_archive_dir=archive,
        **({"runner_type": entries.runner_type, "top_n_candidates": 0} if entries else {}),
    )
    history.to_parquet(runner.cfg.data_dir / "recent_daily_history.parquet", index=False)
    # V21 can emit exit instructions even when JSF fails. Keep this status for recovery.
    with redirect_stdout(StringIO()) as messages:
        runner.run_morning_batch(window[-1], target)
    credit_ok = bool(jsf) and "日証金確認待ち" not in messages.getvalue()
    if (not credit_ok and now < cutoff) or (
        jsf and credit_receipts(project, window[-2:], min(now, cutoff)) != jsf
    ):
        raise ValueError("v21_credit_not_verified")
    factors, blocked, errors = runner._load_corporate_actions_for_date(target)
    if errors:
        raise ValueError("v21_invalid_corporate_actions")
    orders = pd.read_csv(
        runner.cfg.output_dir / f"orders_{target}.csv",
        encoding="utf-8-sig",
        dtype=str,
        keep_default_na=False,
    )
    state = json.loads(runner.cfg.state_path.read_bytes())
    if state["cash_audit_hold"]:
        raise ValueError("v21_cash_audit_hold")
    rows = orders.to_dict("records")
    extra = (
        entries.complete(runner, state, rows, credit_ok, set(factors) | blocked, history)
        if entries
        else {}
    )
    return dict(
        **extra,
        credit_ready=credit_ok,
        split_factors=factors,
        blocked_splits=sorted(blocked),
        orders=rows,
        native=state,
        price_and_master_sources=refs,
        jsf_sources=jsf,
        seed_sha256=SEED_SHA,
        source_sha256=signature(HASHES),
    )


def install(project, release):
    from trading_system.storage.store import atomic_write

    files = {name + ".py": (release / (name + ".py"), digest) for name, digest in HASHES.items()}
    files["seed.parquet"] = (release / "data/recent_daily_history.parquet", SEED_SHA)
    content = {name: verified(src, digest) for name, (src, digest) in files.items()}
    dest = engine_root(project)
    for name, (_, digest) in files.items():
        if (dest / name).exists():
            verified(dest / name, digest)
    for name, data in content.items():
        if not (dest / name).exists():
            atomic_write(dest / name, data)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Install a verified V21 release for paper trading")
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    install(args.project, args.release)
