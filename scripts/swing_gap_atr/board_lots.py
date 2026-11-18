"""Trading unit evidence for the domestic ordinary-share universe only."""

import csv
from collections import defaultdict
from pathlib import Path


class BoardLots:
    """Resolve the ordinary auction lot; an unverified pre-unification issue returns None."""

    def __init__(self, csv_path):
        self.intervals = defaultdict(list)
        with Path(csv_path).open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                unit = int(row["unit"]) if row["unit"] else None
                self.intervals[row["code"]].append((row["start"], row["end"], unit))

    def unit(self, code, day):
        if day >= "2018-10-01":
            if code == "18680":
                return 1000 if day < "2018-10-12" else None
            return 100
        for start, end, unit in self.intervals.get(code, ()):
            if start <= day <= end:
                return unit
        return None
