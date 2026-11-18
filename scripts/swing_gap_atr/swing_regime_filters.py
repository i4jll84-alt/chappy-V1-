"""Apply pre-specified close-above-SMA20 filters to the frozen V1 swing episodes."""

import argparse
import csv
import gzip
import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from statistics import mean

D = Decimal
RULES = ("none", "topix", "g250", "or", "and")
NAMES = ("基準③・休止なし", "A TOPIX", "B グロース250", "C いずれか", "D 両方")


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, data):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(data[0]))
        writer.writeheader()
        writer.writerows(data)


def source_rows(db, hist, evidence, endpoint, start, end):
    parts = db.execute(
        "SELECT raw_path, sha256 FROM bulk_files WHERE endpoint=? AND state='complete' "
        "AND min_date<=? AND max_date>=? ORDER BY min_date,raw_path",
        (endpoint, end, start),
    ).fetchall()
    if not parts:
        raise ValueError(f"No checked bulk data for {endpoint}")
    for rel, expected in parts:
        path = (hist / rel).resolve()
        if not path.is_relative_to(hist) or sha(path) != expected:
            raise ValueError(f"Unverified source {rel}")
        evidence[rel] = expected
        with gzip.open(path, mode="rt", encoding="utf-8-sig", newline="") as stream:
            for r in csv.DictReader(stream):
                if start <= r["Date"] <= end:
                    yield r


def fetch_indices(project, first, last, evidence):
    hist = (project / "data/jquants/history").resolve()
    database = hist / "history.sqlite"
    wal = hist / "history.sqlite-wal"
    if wal.is_file() and wal.stat().st_size:
        raise ValueError("Uncheckpointed source catalog")
    with sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        calendar = list(source_rows(db, hist, evidence, "/markets/calendar", "2008-01-01", last))
        sessions = sorted(r["Date"] for r in calendar if r["HolDiv"] in ("1", "2"))
        if len(sessions) != len(set(sessions)):
            raise ValueError("Duplicate exchange sessions")
        start_index = sessions.index(first)
        if start_index < 19:
            raise ValueError("Insufficient exchange-calendar history")
        span = sessions[start_index - 19 :]
        values = {"topix": {}, "g250": {}, "topix_other": {}}
        for endpoint in ("/indices/bars/daily/topix", "/indices/bars/daily"):
            for row in source_rows(db, hist, evidence, endpoint, span[0], span[-1]):
                if endpoint.endswith("/topix"):
                    name = "topix"
                else:
                    name = {"0070": "g250", "0000": "topix_other"}.get(row["Code"])
                if name is None:
                    continue
                value = D(row["C"]) if row["C"] else None
                if row["Date"] in values[name] or (
                    value is not None and not (value.is_finite() and value > 0)
                ):
                    raise ValueError(f"Duplicate or invalid index close {name} {row['Date']}")
                values[name][row["Date"]] = value
    if any(set(series).difference(span) for series in values.values()):
        raise ValueError("Index observation outside exchange sessions")
    if any(
        values["topix"][d] is not None
        and values["topix_other"][d] is not None
        and values["topix"][d] != values["topix_other"][d]
        for d in values["topix"].keys() & values["topix_other"].keys()
    ):
        raise ValueError("TOPIX disagrees between the two bulk endpoints")
    metrics = {}
    for i in range(19, len(span)):
        date = span[i]
        metrics[date] = {}
        for name in ("topix", "g250"):
            prices = values[name]
            observed = [prices.get(day) for day in span[i - 19 : i + 1]]
            sma = sum(observed) / 20 if all(x is not None for x in observed) else None
            metrics[date][name + "_close"] = prices.get(date)
            metrics[date][name + "_ma20"] = sma
            metrics[date]["filter_" + name] = int(prices[date] > sma) if sma is not None else None
    return metrics, span


def evaluate(group, key, label):
    def avg(seq):
        return mean(seq) if seq else None

    full = [r["pnl_s3"] for r in group]
    active = [r["pnl_s3"] for r in group if r["filter_" + key]]
    skipped = [r["pnl_s3"] for r in group if not r["filter_" + key]]
    net = [r["pnl_" + key] for r in group]
    dd = [r["dd_" + key + "_pct"] for r in group]
    diff = mean(net) - mean(full)
    if abs(diff + sum(skipped, D(0)) / len(full)) > D("1e-19"):
        raise AssertionError("Skipped cashflows do not reconcile")
    return dict(
        filter=key,
        label=label,
        n=len(group),
        active=len(active),
        rate_pct=D(100) * len(active) / len(group),
        mean_pnl=mean(net),
        delta_mean_pnl=diff,
        active_mean_pnl=avg(active),
        inactive_baseline_mean_pnl=avg(skipped),
        avoided_mean_pnl_per_inactive=-mean(skipped) if skipped else None,
        worst_pnl=min(net),
        mean_max_dd_pct=mean(dd),
        active_mean_max_dd_pct=avg([r["max_dd_s3_pct"] for r in group if r["filter_" + key]]),
        inactive_winning=sum(x > 0 for x in skipped),
        inactive_losing=sum(x < 0 for x in skipped),
    )


def main(project, out):
    prev = project / "data/offline-verification/swing-gap-atr-20261118/episodes"
    frozen = rows(prev / "paired.csv")
    dates = [r["date"] for r in frozen]
    if dates != sorted(set(dates)) or len(dates) != 730:
        raise ValueError("Unexpected original decision sequence")
    old = {r["date"]: r for r in rows(prev / "windows.csv") if r["rule"] == "guarded"}
    keep = [
        r for r in frozen if all(r[k + "_status"] == "ok" for k in ("current", "plus3", "guarded"))
    ]
    if len(keep) != 709:
        raise ValueError("Frozen matched sample changed")
    refs = {}
    market, span = fetch_indices(project, dates[0], dates[-1], refs)
    if list(market) != dates:
        raise ValueError("Decision slots disagree with the exchange calendar")
    indices = {d: i for i, d in enumerate(dates)}
    episodes = []
    missing = {}
    for row in keep:
        d = row["date"]
        gaps = [k for k in ("topix", "g250") if market[d][k + "_ma20"] is None]
        if gaps:
            missing[d] = gaps
            continue
        nav = old[d]
        assert nav["status"] == "ok" and D(nav["net"]) == D(row["guarded_net"])
        rec = dict(
            select_date=d,
            year=int(d[:4]),
            slot=indices[d],
            status_s3="ok",
            pnl_s3=D(nav["net"]),
            max_dd_s3=D(nav["dd_pct"]) / 100,
            max_dd_s3_pct=D(nav["dd_pct"]),
            **market[d],
        )
        t, g = rec["filter_topix"], rec["filter_g250"]
        rec.update(filter_none=1, filter_or=int(t or g), filter_and=int(t and g))
        for rule in RULES:
            active = rec["filter_" + rule]
            rec["pnl_" + rule] = rec["pnl_s3"] * active
            rec["dd_" + rule + "_pct"] = rec["max_dd_s3_pct"] * active
        episodes.append(rec)
    if not episodes:
        raise ValueError("No matched dates with twenty consecutive closes for both indices")
    summary = [evaluate(episodes, k, n) for k, n in zip(RULES, NAMES, strict=True)]
    years = sorted({r["year"] for r in episodes})
    by_year = [
        dict(year=y, **evaluate([r for r in episodes if r["year"] == y], k, n))
        for y in years
        for k, n in zip(RULES, NAMES, strict=True)
    ]
    offsets = [
        dict(start_offset=i, **evaluate([r for r in episodes if r["slot"] % 7 == i], k, n))
        for i in sorted({r["slot"] % 7 for r in episodes})
        for k, n in zip(RULES, NAMES, strict=True)
    ]
    cells = []
    for t in (0, 1):
        for g in (0, 1):
            sample = [r for r in episodes if r["filter_topix"] == t and r["filter_g250"] == g]
            cells.append(
                dict(
                    topix_above=t,
                    g250_above=g,
                    n=len(sample),
                    mean_baseline_pnl=mean(r["pnl_s3"] for r in sample) if sample else None,
                )
            )
    out.mkdir(parents=True, exist_ok=True)
    for name, data in (
        ("summary", summary),
        ("by_year", by_year),
        ("episodes", episodes),
        ("nonoverlap", offsets),
        ("index_states", cells),
    ):
        save(out / ("regime_" + name + ".csv"), data)
    evidence = dict(
        archive_first=span[0],
        archive_last=span[-1],
        verified_sessions=len(span),
        candidates=len(frozen),
        evaluated=len(episodes),
        excluded_missing_index_windows=missing,
        baseline_files={f: sha(prev / f) for f in ("paired.csv", "windows.csv")},
        index_codes={"topix": "0000", "growth250": "0070"},
        close_sources=refs,
    )
    (out / "regime_sources.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for r in summary:
        print(
            r["label"], r["active"], r["n"], round(r["mean_pnl"], 2), round(r["delta_mean_pnl"], 2)
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    main(args.project.resolve(), args.out.resolve())
