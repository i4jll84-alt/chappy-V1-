"""Verified ten-year J-Quants quote window for the continuous swing account."""

import json
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path, PureWindowsPath

import duckdb
from continuous import Market, read, validate_hash


class DecadeMarket(Market):
    """Use the original ATR and monthly OHLC reader across the full bulk-price archive."""

    def __init__(self, project, codes):
        self.project = Path(project).resolve()
        jq = self.project / "data/jquants"
        self.root = jq / "history"
        self.codes = set(codes)
        quality = read(self.root / "quality-20261031.json")
        if (quality["start"], quality["end"]) != ("2016-10-03", "2026-10-30") or any(
            quality["checks"].values()
        ):
            raise ValueError("Unexpected date range or quality checks for the bulk archive")
        self.evidence = {}
        self.used_hashes = {}
        self.files = defaultdict(list)
        calendars = []
        for item in quality["input_manifest"]:
            endpoint = item["endpoint"]
            if endpoint not in ("/equities/bars/daily", "/markets/calendar"):
                continue
            path, sha = item["parquet_path"], item["parquet_sha256"]
            if item["state"] != "complete":
                raise ValueError(f"Incomplete verified bulk file: {path}")
            key = str((self.root / path).relative_to(self.project)).replace("/", chr(92))
            self.evidence[key] = sha
            if endpoint == "/markets/calendar":
                calendars.append(self.checked(path, sha))
            else:
                # Monthly archive objects use YYYYMM; the live tail uses YYYYMMDD.
                digits = (
                    Path(item["object_key"]).name.removeprefix("equities_bars_daily_").split(".")[0]
                )
                if len(digits) not in (6, 8) or not digits.isdigit():
                    raise ValueError(f"Unrecognized date in {item['object_key']}")
                self.files[f"{digits[:4]}-{digits[4:6]}"].append((path, sha))
        self.db = duckdb.connect()
        self.db.execute("SET threads=2")
        self.db.execute("CREATE TEMP TABLE needed (Code VARCHAR PRIMARY KEY)")
        if self.codes:
            self.db.executemany("INSERT INTO needed VALUES (?)", [(c,) for c in sorted(self.codes)])
        self.db.from_parquet(calendars).create_view("calendar")
        self.sessions = [
            day
            for (day,) in self.db.execute(
                "SELECT Date FROM calendar WHERE HolDiv IN ('1','2') ORDER BY Date"
            ).fetchall()
        ]
        self.indices = {day: i for i, day in enumerate(self.sessions)}
        assert len(self.indices) == len(self.sessions)
        evidence = (
            self.project / "data/offline-verification/swing-position-caps-20261118/inputs.json"
        )
        self.snapshots = {}
        for k, sha in read(evidence)["sources"].items():
            parts = PureWindowsPath(k).parts
            if parts[0] == "daily-snapshots":
                day = parts[1]
                if day in self.snapshots:
                    raise ValueError(f"More than one pinned daily snapshot on {day}")
                self.snapshots[day] = jq.joinpath(*parts), k, sha
        required = [d for d in self.sessions if "2026-10-30" < d <= "2026-11-18"]
        if sorted(self.snapshots) != required:
            raise ValueError("November daily manifest chain is incomplete")

    @lru_cache(maxsize=32)  # noqa: B019 - one market instance per replay process
    def bars(self, day):
        if day <= "2026-10-30":
            return self.monthly(day[:7]).get(day, {})
        from trading_system.research.swing_daily import verify_pages

        path, key, sha = self.snapshots[day]
        validate_hash(path, sha)
        manifest = read(path)
        count, _ = verify_pages(path.parent, manifest, day)
        if manifest["state"] != "verified" or not count or count != manifest["rows"]:
            raise ValueError(f"Unverified daily cross-section: {day}")
        close_cutoff = datetime.fromisoformat(day + "T23:59:59+09:00")
        quotes = {}
        for page in manifest["pages"]:
            if datetime.fromisoformat(page["available_at"]) > close_cutoff:
                raise ValueError(f"Daily prices became available after selection cutoff: {day}")
            for quote in json.loads((path.parent / page["file"]).read_bytes())["data"]:
                code = quote["Code"]
                if code in self.codes:
                    quotes[code] = quote
        self.used_hashes[key] = sha
        return quotes
