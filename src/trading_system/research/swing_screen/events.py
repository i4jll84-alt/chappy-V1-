"""Disclosure-day groups and post-disclosure price labels, completely separate from ranking."""

from bisect import bisect_left, bisect_right
from collections import defaultdict

import pandas as pd

from .portfolio import good


class EventClock:
    def __init__(self, folder):
        fin = pd.read_parquet(folder / "financial.parquet")
        self.reports = (
            fin.loc[fin.DocType.str.contains("FinancialStatements", na=False)]
            .groupby("DiscDate")
            .Code.agg(set)
            .to_dict()
        )
        self.revisions = (
            fin.loc[fin.DocType.str.contains("EarnForecastRevision", na=False)]
            .groupby("DiscDate")
            .Code.agg(set)
            .to_dict()
        )
        schedule = pd.read_parquet(folder / "schedule.parquet")
        self.announcements = defaultdict(dict)
        for (d, c), g in schedule.groupby(["PubDate", "Code"]):
            unique = g.SchDate.dropna().unique()
            self.announcements[d][c] = str(unique[0]) if len(unique) == 1 else None
        self.publications = sorted(self.announcements)
        self.disclosures = sorted(self.reports.keys() | self.revisions.keys())
        self.upcoming = {}
        self.recent = {}
        self.labels = []
        self.jobs = []
        self.prev = None

    def screen(self, day, i, cal, market, before, ranks):
        # Disclosures on the decision date are conservatively first actionable next session.
        start = bisect_left(self.publications, self.prev) if self.prev else 0
        for published in self.publications[start : bisect_left(self.publications, day)]:
            self.upcoming.update(self.announcements[published])
        keep = []
        for job in self.jobs:
            age = i - job["index"]
            q = market.get(job["code"])
            if not q or not good(q.C) or q.AdjFactor != 1 or q.ExRT not in ("", None, "0"):
                job["valid"] = False
            if age in (1, 5):
                self.labels.append(
                    dict(
                        date=job["date"],
                        code=job["code"],
                        age=age,
                        revision=job["revision"],
                        before_to_event=job["event_return"],
                        after_return=q.C / job["anchor"] - 1
                        if job["valid"] and good(q.C)
                        else None,
                    )
                )
            if age < 5:
                keep.append(job)
        self.jobs = keep
        start = (
            bisect_right(self.disclosures, self.prev)
            if self.prev
            else bisect_left(self.disclosures, day)
        )
        released = self.disclosures[start : bisect_right(self.disclosures, day)]
        filings = set().union(*(self.reports.get(d, set()) for d in released))
        revisions = set().union(*(self.revisions.get(d, set()) for d in released))
        for c in sorted(filings | revisions):
            q = market.get(c)
            b = before.get(c)
            if (
                not q
                or not good(q.C)
                or not b
                or not good(b.C)
                or q.AdjFactor != 1
                or q.ExRT not in ("", None, "0")
            ):
                continue
            self.jobs.append(
                dict(
                    index=i,
                    date=day,
                    code=c,
                    anchor=q.C,
                    valid=True,
                    revision=c in revisions,
                    event_return=q.C / b.C - 1,
                )
            )
        self.recent = {
            c: anchor
            for c, anchor in self.recent.items()
            if i - anchor[0] <= 5
            and (q := market.get(c))
            and good(q.C)
            and q.AdjFactor == 1
            and q.ExRT in ("", None, "0")
        }
        for c in filings:
            q = market.get(c)
            if q and good(q.C):
                self.recent[c] = (i, q.C)
        end = cal[min(i + 5, len(cal) - 1)]
        for m in "ABCD":
            ranks[m + "_before_earnings"] = [
                c for c in ranks[m] if day < (self.upcoming.get(c) or "") <= end
            ]
            ranks[m + "_after_earnings"] = [
                c
                for c in ranks[m]
                if c in self.recent
                and 0 < i - self.recent[c][0] <= 5
                and c in market
                and market[c].C > self.recent[c][1]
            ]
        self.prev = day
