"""Forward-only paper books entered from the first immutable, pre-open publication."""

from datetime import UTC, datetime, time

import pandas as pd

from ...brokers.locking import single_writer
from .inputs import Sources, digest, settings
from .portfolio import Book, session
from .prepare import JST, calendar, daily, load, root, write
from .study import CORE, price_map


def freeze(project, day, **kwargs):
    return daily(project, day, **kwargs)


def settle(project, through, now=None):
    now = now or datetime.now(UTC)
    home = root(project)
    cfg = settings(project)
    if (home / "STOP").exists():
        return {"state": "stopped"}
    plans = sorted((home / "daily").glob("*/first.json"))
    if not plans:
        return {"state": "awaiting_first_frozen_selection"}
    first = load(plans[0])["target"]
    if through < first:
        return {"state": "awaiting_first_open", "target": first}
    src = Sources(project, now)
    cal = calendar(src, first, through)
    folder = home / "paper"
    folder.mkdir(parents=True, exist_ok=True)
    with single_writer(folder):
        last = None
        for day in [d for d in cal if first <= d <= through]:
            file = folder / (day + ".json")
            if file.is_file():
                last = load(file)
                continue
            if day not in src.receipts:
                return {"state": "awaiting_verified_prices", "date": day}
            idx = cal.index(day)
            prev = cal[idx - 1]
            sel = home / "daily" / prev / "first.json"
            ranks = {}
            prior = {}
            source = None
            if sel.is_file():
                meta = load(sel)
                if (
                    meta["mode"] != "forward"
                    or meta["target"] != day
                    or datetime.fromisoformat(meta["published"]).astimezone(JST)
                    >= datetime.combine(pd.Timestamp(day).date(), time(9), JST)
                ):
                    raise ValueError("paper input was not frozen before opening")
                base = sel.parent / meta["version"]
                source = meta["version"]
                persisted = load(base / "inputs.json")
                if (
                    digest(base / "inputs.json") != source
                    or digest(base / "features.parquet") != meta["features_sha256"]
                ):
                    raise ValueError("frozen candidates hash mismatch")
                if persisted["config"] != cfg:
                    return {"state": "paper_config_change_requires_new_book", "date": day}
                if load(base / "summary.json") != meta:
                    raise ValueError("first publication mismatch")
                if last and last["config"] != cfg:
                    raise ValueError("paper book version mismatch")
                prior = price_map(pd.read_parquet(base / "features.parquet").reset_index())
                ranks = meta["candidates"]
            raw, _ = src.blocks([day])
            raw["atr"] = float("nan")
            raw["ma20"] = float("nan")
            market = price_map(raw)
            accounts = {}
            # The exchange-session number must stay stable across daily calls.
            absolute = pd.Timestamp(day).toordinal()
            origin = cal.index(first)
            for c in cfg["capital_levels"]:
                for h in cfg["holds"]:
                    for method in CORE:
                        key = f"{method}:{c}:{h}"
                        book = (
                            Book.restore(last["accounts"][key]["book"]) if last else Book.start(c)
                        )
                        nav, fills, skips = session(
                            book,
                            idx - origin,
                            day,
                            prior,
                            market,
                            ranks.get(method, []),
                            cfg,
                            dict(hold=h),
                        )
                        for pos in book.positions.values():
                            pos.sector = str(pos.sector)
                        accounts[key] = dict(book=book.dump(), nav=nav, fills=fills, skips=skips)
            last = dict(
                state="settled",
                date=day,
                selection=source,
                config=cfg,
                accounts=accounts,
                missing_selection=not bool(source),
                inputs=src.refs,
                ordinal=absolute,
            )
            write(file, last)
    src.daily.cache_clear()
    return (
        {"state": "settled", "through": through, "accounts": len(last["accounts"])}
        if last
        else {"state": "holiday"}
    )


def after_daily(project, day):
    if not (project / "config/swing-screen.json").exists():
        return None
    if (root(project) / "STOP").is_file():
        return {"state": "stopped"}
    result = {}
    for k, fn in (("paper", settle), ("selection", freeze)):
        try:
            r = fn(project, day)
            result[k] = {
                key: r[key] for key in ("state", "date", "target", "through", "version") if key in r
            }
        except Exception as e:
            result[k] = {"state": "pending", "reason": str(e)[:240]}
    return result
