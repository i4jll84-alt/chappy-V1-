"""Check all prior daily candidate ATRs and quoted bars, plus the pre-2023 full calendar."""

import argparse
import csv
import json
import sys
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()
    r = args.project.resolve()
    sys.path.insert(0, str(r / "src"))
    sys.path.insert(0, str(r / "scripts/swing_gap_atr"))
    from continuous import Market
    from decade_market import DecadeMarket

    cap = r / "data/offline-verification/swing-position-caps-20261118"
    with (cap / "candidates.csv").open(encoding="utf-8-sig", newline="") as f:
        choices = list(csv.DictReader(f))
    pairs = {(row["signal"], row["code"]) for row in choices}
    codes = {c for _, c in pairs}
    study = r / "data/offline-verification/swing-periods-ma-jsf-20261113/outputs"
    new = DecadeMarket(r, codes)
    cutoffs = {d: d + "T23:59:59+09:00" for d in new.sessions if "2026-10-30" < d <= "2026-11-18"}
    legacy = Market(r, study, codes, cutoffs, {})
    assert legacy.sessions == new.sessions
    date_choices = {}
    for d, c in sorted(pairs):
        date_choices.setdefault(d, []).append(c)
    total = common = occurrences = observed = 0
    keys = ("O", "H", "L", "C", "AdjFactor", "ExRT")
    for d in new.sessions[new.indices["2016-11-18"] : new.indices["2026-11-18"] + 1]:
        q = new.bars(d)
        total += 1
        observed += len(q)
        if d >= "2023-10-31":
            old = legacy.bars(d)
            assert set(q) == set(old), (d, len(q), len(old))
            for c, bar in q.items():
                assert all(bar.get(k) == old[c].get(k) for k in keys), (d, c, bar, old[c])
            common += len(old)
            for c in date_choices.get(d, ()):
                assert legacy.atr(d, c) == new.atr(d, c), (d, c)
                occurrences += 1
    assert total == 2441 and occurrences == len(pairs)
    for k, v in legacy.used_hashes.items():
        assert new.used_hashes[k] == v
    record = {
        "mark_dates": total,
        "full_quote_rows": observed,
        "overlap_quotes": common,
        "overlap_atrs": occurrences,
        "unique_candidate_codes": len(codes),
        "source_digests": len(new.used_hashes),
        "first_new_date": new.sessions[new.indices["2016-11-18"]],
        "last_mark": "2026-11-18",
    }
    args.out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf8")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
