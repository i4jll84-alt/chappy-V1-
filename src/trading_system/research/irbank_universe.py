"""Freeze the requested liquidity universe before observing IRBANK results."""

import hashlib
import json
from pathlib import Path

import duckdb

from trading_system.storage.store import atomic_write

START = "2016-11-20"
END = "2023-10-31"
AUX_START = "2016-11-17"


def sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def prepare(project, root):
    target = Path(root) / "universe.json"
    if target.exists():
        if not target.with_suffix(".sha256").exists():
            raise ValueError("universe_pin_missing")
        if sha(target) != target.with_suffix(".sha256").read_text(encoding="ascii").strip():
            raise ValueError("universe_pin_mismatch")
        result = json.loads(target.read_bytes())
        if (result["start"], result["end"]) != (START, END):
            raise ValueError("universe_date_mismatch")
        return result
    history = Path(project) / "data/jquants/history"
    manifest = history / "quality-20261031.json"
    meta = json.loads(manifest.read_bytes())
    if any(meta["checks"].values()):
        raise ValueError("history_quality_not_passed")
    names = {"/equities/bars/daily": "bars", "/equities/master": "master"}
    files = {name: [] for name in names.values()}
    pins = {}
    for row in meta["input_manifest"]:
        name = names.get(row["endpoint"])
        if name is None:
            continue
        path = history / row["parquet_path"]
        digest = sha(path)
        if row["state"] != "complete" or digest != row["parquet_sha256"]:
            raise ValueError("history_hash_mismatch")
        pins[str(path.relative_to(project))] = digest
        files[name].append(str(path))
    with duckdb.connect() as db:
        db.execute("SET threads=2")
        for name, paths in files.items():
            db.from_parquet(paths).create_view(name)
        rows = db.execute(
            """
            SELECT b.Code, min(b.Date), max(b.Date), count(*),
              count(*) FILTER (WHERE b.Date>=?)
            FROM bars b JOIN master m USING(Date,Code)
            WHERE b.Date BETWEEN ? AND ? AND m.ProdCat='011' AND right(b.Code,1)='0'
              AND try_cast(b.Va AS DECIMAL(23,0))>=100000000
              AND ((b.Date<'2022-04-04' AND m.Mkt IN ('0101','0102','0104','0106','0107'))
                OR (b.Date>='2022-04-04' AND m.Mkt IN ('0111','0112','0113')))
            GROUP BY b.Code ORDER BY b.Code
        """,
            [START, AUX_START, END],
        ).fetchall()
    codes = [r[0][:-1] for r in rows if r[4]]
    auxiliary = [r[0][:-1] for r in rows if not r[4]]
    from trading_system.research.swing_credit import tokyo

    official_root = Path(project) / "data/free-market/jsf-history"
    receipt = json.loads((official_root / "days/2026-10-30.json").read_bytes())
    csv_path = official_root / receipt["csv"]["path"]
    if sha(csv_path) != receipt["csv"]["sha256"]:
        raise ValueError("official_sample_hash_mismatch")
    official = tokyo(csv_path.read_bytes())
    eligible = [
        code
        for code in codes
        if code + "0" in official
        and any(
            int(official[code + "0"].get(k, "").replace(",", "") or 0) > 0
            for k in ("融資残高（株）", "貸株残高（株）")
        )
    ]
    ranked = sorted(
        eligible, key=lambda c: hashlib.sha256(("irbank-audit-v1:" + c).encode()).hexdigest()
    )
    sample = ["7203"] + [c for c in ranked if c != "7203"][:99]
    if "7203" not in eligible or len(sample) != 100:
        raise ValueError("insufficient_official_audit_sample")
    result = {
        "version": 1,
        "start": START,
        "end": END,
        "history_start": AUX_START,
        "history_end": END,
        "selection": "TSE domestic common stocks; same-day master; Va >= 100000000 at least once",
        "codes": codes,
        "boundary_only_codes": auxiliary,
        "liquid_dates": {r[0][:-1]: {"first": r[1], "last": r[2], "days": r[3]} for r in rows},
        "audit_codes": sample,
        "audit_start": "2024-01-01",
        "audit_end": "2026-11-20",
        "audit_selection": (
            "7203 + 99 smallest SHA256(irbank-audit-v1:code); official nonzero balances"
        ),
        "source_manifest_sha256": sha(manifest),
        "source_sha256": pins,
        "audit_official_sha256": sha(csv_path),
        "estimated_full_pages": 7 * len(rows) + 3 * len(sample),
        "estimate_note": "Continuous coverage estimate; 404 and new listings can reduce pages",
    }
    atomic_write(target, json.dumps(result, ensure_ascii=False, indent=2).encode())
    atomic_write(target.with_suffix(".sha256"), sha(target).encode())
    return result
