"""Fixed-rule regression fixtures from the pinned A0_5 source, with V1's paper book."""

import unittest
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from trading_system.research.a0_5_book import initial
from trading_system.research.a0_5_rules.backtester import execute, simulate
from trading_system.research.a0_5_rules.config import Config
from trading_system.research.a0_5_rules.data_fetcher import iso
from trading_system.research.a0_5_rules.data_processor import (
    current_weekly_reference,
    features,
    lag_join,
    margin_events,
)
from trading_system.research.a0_5_rules.live_portfolio import buying_power
from trading_system.research.a0_5_rules.live_screen import freshness, latest_close, plan
from trading_system.research.a0_5_rules.strategy import Position, orders


def trading_sessions(calendar, start, end):
    """Return TSE sessions after checking calendar continuity, including the next session."""
    cal = pd.DataFrame(calendar, columns=["Date", "HolDiv"])
    divisions = cal.HolDiv.astype("string")
    if (
        cal.Date.isna().any()
        or cal.Date.duplicated().any()
        or not divisions.isin(["0", "1", "2", "3"]).all()
    ):
        raise ValueError("取引カレンダーの日付または休日区分が不正です。")
    sessions = sorted(cal.loc[divisions.isin(["1", "2"]), "Date"])
    if not sessions or sessions[-1] <= iso(end):
        raise ValueError("翌営業日を確認できません。")
    # An absent date between 'end' and the next returned session could itself be a session.
    expected = pd.date_range(start, sessions[-1]).strftime("%Y-%m-%d")
    if set(expected).difference(cal.Date):
        raise ValueError("取引カレンダーに欠落があります。")
    return sessions


def october_calendar():
    # Fri 9th, Tue 13th, Wed 14th, Thu 15th. Monday is closed for equities.
    return [
        {"Date": f"2026-10-{day:02}", "HolDiv": div}
        for day, div in [(9, "1"), (10, "0"), (11, "0"), (12, "3"), (13, "1"), (14, "1"), (15, "1")]
    ]


def releases_fixture():
    sessions = pd.to_datetime(trading_sessions(october_calendar(), "2026-10-09", "2026-10-14"))
    quotes = pd.MultiIndex.from_product(
        [sessions, ["10000", "20000"]], names=["Date", "Code"]
    ).to_frame(index=False)
    quotes["Code"] = quotes.Code.astype("string")
    # A Friday correction to the first company's June results.
    filings = [
        ("10000", "2025-08-01", "15:00:00", 2025, 80),
        ("10000", "2026-08-03", "15:00:00", 2026, 100),
        ("10000", "2026-10-09", "09:00:00", 2026, 120),
        ("20000", "2026-08-03", "15:00:00", 2026, 70),
    ]
    finance = pd.DataFrame(
        [
            {
                "Code": code,
                "DiscDate": pd.Timestamp(pub),
                "DiscTime": time,
                "Sales": sales,
                "OP": 10,
                "DocType": "1QFinancialStatements_Consolidated_JP",
                "CurPerType": "1Q",
                "CurPerSt": pd.Timestamp(year, 4, 1),
                "CurPerEn": pd.Timestamp(year, 6, 30),
            }
            for code, pub, time, year, sales in filings
        ]
    )
    margin = pd.DataFrame(
        {
            "Code": ["10000", "10000", "20000"],
            "LongVol": [50, 90, 60],
            "Date": pd.to_datetime(["2026-10-05", "2026-10-07", "2026-10-05"]),
            "PubDate": pd.to_datetime(["2026-10-06", "2026-10-09", "2026-10-06"]),
        }
    )
    investors = pd.DataFrame(
        {
            "PubDate": pd.to_datetime(["2026-10-02", "2026-10-09"]),
            "EnDate": pd.to_datetime(["2026-09-25", "2026-10-02"]),
            "Section": "TSEPrime",
            "FrgnBal": [-10, 20],
        }
    )
    raw = {
        "bars": quotes.assign(
            O=100.0, H=101.0, L=99.0, C=100.0, Vo=1000, Va=100000, AdjFactor=1.0, ExRT=None
        ),
        "master": quotes.assign(ProdCat="011", S17="1", S33="3650", Mkt="0111"),
        "topix": pd.DataFrame({"Date": sessions, "C": 100.0}),
        "financial": finance.astype({"Code": "string"}),
        "margin": margin.astype({"Code": "string"}),
        "investors": investors,
    }
    return raw, sessions


class PublicationLagTests(unittest.TestCase):
    def test_friday_release_first_visible_on_tuesday(self):
        sessions = pd.DataFrame({"Date": pd.to_datetime(["2026-10-09", "2026-10-13"])})
        releases = pd.DataFrame({"PubDate": pd.to_datetime(["2026-10-09"]), "LongVol": [90]})
        got = lag_join(sessions, releases, "PubDate")
        self.assertTrue(pd.isna(got.loc[0, "LongVol"]))
        self.assertEqual(got.loc[1, "LongVol"], 90)

    def test_all_three_sources_change_after_publication_and_only_for_relevant_codes(self):
        raw, sessions = releases_fixture()
        table = features(raw, sessions, Config(window=2, exit_window=2))
        first = table.loc[table.Code.eq("10000")].set_index("Date").sort_index()
        self.assertEqual(first.Sales.tolist(), [100, 120, 120, 120])
        self.assertEqual(first.LongVol.tolist(), [50, 90, 90, 90])
        self.assertEqual(first.FrgnBal.tolist(), [-10, 20, 20, 20])
        for col in ["FinDate", "MarginDate", "FlowDate"]:
            self.assertEqual(first.loc["2026-10-13", col], pd.Timestamp("2026-10-09"))
            self.assertTrue((first[col] < first.index).all())
        second = table.loc[table.Code.eq("20000")]
        self.assertTrue(second.Sales.eq(70).all())
        self.assertTrue(second.LongVol.eq(60).all())
        self.assertFalse(second.loc[second.Date >= sessions[1], "MarginKnown"].any())

    def test_missing_publications_outside_calendar_are_unavailable(self):
        raw, sessions = releases_fixture()
        for feed, date_col in [
            ("financial", "DiscDate"),
            ("margin", "PubDate"),
            ("investors", "PubDate"),
        ]:
            raw[feed][date_col] = pd.NaT
        day = features(raw, sessions, Config())
        self.assertTrue(day[["Sales", "LongVol", "FrgnBal"]].isna().all().all())
        self.assertFalse(day[["FinKnown", "MarginKnown", "FlowKnown", "Eligible"]].any().any())

    def test_estimated_margin_crosses_holiday_and_first_joins_fourth_session(self):
        raw, _ = releases_fixture()
        sessions = pd.bdate_range("2026-07-13", "2026-07-28").difference(
            pd.to_datetime(["2026-07-20"])
        )
        quotes = pd.MultiIndex.from_product(
            [sessions, ["10000", "20000"]], names=["Date", "Code"]
        ).to_frame(index=False)
        quotes["Code"] = quotes.Code.astype("string")
        raw["bars"] = quotes.assign(
            O=100.0, H=101.0, L=99.0, C=100.0, Vo=1000, Va=100000, AdjFactor=1.0, ExRT=None
        )
        raw["master"] = quotes.assign(ProdCat="011", S17="1", S33="3650", Mkt="0111")
        raw["topix"] = pd.DataFrame({"Date": sessions, "C": 100.0})
        raw["margin"] = pd.DataFrame(
            {
                "Code": pd.Series(["10000", "20000"], dtype="string"),
                "Date": pd.to_datetime(["2026-07-17"] * 2),
                "PubDate": pd.to_datetime([None, "2026-07-24"]),
                "LongVol": [80.0, 90.0],
            }
        )
        cfg = Config(window=2, exit_window=2)
        day = features(raw, sessions, cfg)
        estimated = day[day.Code.eq("10000")].set_index("Date")
        self.assertTrue(estimated.loc[:"2026-07-23", "LongVol"].isna().all())
        row = estimated.loc["2026-07-24"]
        self.assertEqual(row.LongVol, 80)
        self.assertTrue(row.MarginKnown and row.is_published_date_estimated)
        self.assertTrue(pd.isna(row.MarginPubDate))
        self.assertEqual(row.MarginDate, pd.Timestamp("2026-07-23"))
        actual = day[day.Code.eq("20000")].set_index("Date")
        self.assertTrue(actual.loc[:"2026-07-24", "LongVol"].isna().all())
        self.assertFalse(actual.loc["2026-07-27", "is_published_date_estimated"])
        self.assertEqual(actual.loc["2026-07-27", "MarginPubDate"], pd.Timestamp("2026-07-24"))
        for cutoff in ["2026-07-23", "2026-07-24"]:
            short = features(raw, sessions[sessions <= cutoff], cfg)
            cols = ["Code", "LongVol", "MarginKnown", "MarginDate", "is_published_date_estimated"]
            pd.testing.assert_frame_equal(
                short.loc[short.Date.eq(cutoff), cols].set_index("Code").sort_index(),
                day.loc[day.Date.eq(cutoff), cols].set_index("Code").sort_index(),
            )

    def test_weekly_estimates_expire_when_a_scheduled_week_is_missing(self):
        sessions = pd.bdate_range("2026-04-20", "2026-05-25").difference(
            pd.to_datetime(["2026-04-29", "2026-05-04", "2026-05-05", "2026-05-06"])
        )
        latest = current_weekly_reference(sessions)
        # May 7-8 is a two-session week without a weekly publication.
        self.assertEqual(latest.loc["2026-05-14"], pd.Timestamp("2026-05-01"))
        self.assertEqual(latest.loc["2026-05-20"], pd.Timestamp("2026-05-01"))
        self.assertEqual(latest.loc["2026-05-21"], pd.Timestamp("2026-05-15"))

    def test_later_dated_correction_does_not_erase_earlier_estimate(self):
        sessions = pd.bdate_range("2026-07-06", "2026-07-31")
        base = pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-07-10"] * 2),
                "Code": ["10000"] * 2,
                "PubDate": pd.to_datetime([None, "2026-07-17"]),
                "LongVol": [10, 20],
            }
        )
        pubs = margin_events(base, sessions)
        target = pd.DataFrame({"Date": pd.to_datetime(["2026-07-15", "2026-07-16", "2026-07-21"])})
        seen = lag_join(target, pubs.drop(columns="Code"), "MarginDate")
        self.assertTrue(pd.isna(seen.LongVol.iloc[0]))
        self.assertEqual(seen.LongVol.iloc[1:].tolist(), [10, 20])
        self.assertEqual(seen.is_published_date_estimated.iloc[1:].tolist(), [True, False])
        base.loc[1, "PubDate"] = pd.Timestamp("2026-07-14")
        self.assertEqual(margin_events(base, sessions).LongVol.tolist(), [20])

    def test_old_week_revision_does_not_displace_new_week(self):
        raw, sessions = releases_fixture()
        flow = raw["investors"]
        old_week = flow.iloc[[0]].assign(PubDate=sessions[1], FrgnBal=999)
        latest_week = flow.iloc[[1]].assign(PubDate=sessions[2], FrgnBal=30)
        raw["investors"] = pd.concat([latest_week, old_week, flow], ignore_index=True)
        table = features(raw, sessions, Config())
        actual = table.loc[table.Code.eq("10000")].sort_values("Date").FrgnBal
        self.assertEqual(actual.tolist(), [-10, 20, 20, 30])

    def test_removing_all_as_yet_unpublished_rows_preserves_each_day(self):
        raw, sessions = releases_fixture()
        cfg = Config(window=2, exit_window=2)
        full = features(raw, sessions, cfg)
        for date in sessions:
            with self.subTest(date=date):
                visible = dict(raw)
                for name, col in [
                    ("financial", "DiscDate"),
                    ("margin", "PubDate"),
                    ("investors", "PubDate"),
                ]:
                    visible[name] = raw[name].loc[raw[name][col] < date]
                history = features(visible, sessions[sessions <= date], cfg)
                cols = [
                    "FinDate",
                    "Sales",
                    "MarginDate",
                    "LongVol",
                    "FlowDate",
                    "FrgnBal",
                    "Eligible",
                    "Signal",
                ]

                def on_date(frame):
                    return frame.loc[frame.Date.eq(date)].set_index("Code").sort_index()[cols]

                pd.testing.assert_frame_equal(on_date(full), on_date(history))

    def test_gap_after_end_cannot_skip_the_next_trading_day(self):
        rows = [r for r in october_calendar() if r["Date"] != "2026-10-13"]
        with self.assertRaisesRegex(ValueError, "欠落"):
            trading_sessions(rows, "2026-10-09", "2026-10-09")


def source():
    sessions = pd.bdate_range("2026-09-03", periods=32)
    signal = sessions[27]
    bars, master, margin, financial = [], [], [], []
    for code, sector in [("10000", "1"), ("20000", "2"), ("30000", "3")]:
        for i, day in enumerate(sessions):
            price = 100 + (i - 20 + 12 * (day >= signal) if code == "10000" else 0)
            vol = 30_000_000 if day == signal and code == "10000" else 10_000_000
            bars.append([day, code, price, price + 1, price - 1, price, vol, vol * price, 1, None])
            master.append(
                [
                    day,
                    code,
                    code,
                    sector,
                    sector + "100",
                    "014" if code == "30000" else "011",
                    "0111",
                ]
            )
            margin.append([day - pd.offsets.BDay(1), day, code, 1000])
        for year, sales, op in ((2025, 10, 1), (2026, 11, 2)):
            financial.append(
                [
                    pd.Timestamp(f"{year}-09-01"),
                    "15:30:00",
                    code,
                    str(year),
                    "1QFinancialStatements_Consolidated_JP",
                    "1Q",
                    pd.Timestamp(f"{year}-04-01"),
                    pd.Timestamp(f"{year}-06-30"),
                    sales,
                    op,
                ]
            )
    return sessions, {
        "bars": pd.DataFrame(bars, columns="Date Code O H L C Vo Va AdjFactor ExRT".split()),
        "master": pd.DataFrame(master, columns="Date Code CoName S17 S33 ProdCat Mkt".split()),
        "margin": pd.DataFrame(margin, columns="Date PubDate Code LongVol".split()),
        "financial": pd.DataFrame(
            financial,
            columns=(
                "DiscDate DiscTime Code DiscNo DocType CurPerType CurPerSt CurPerEn Sales OP"
            ).split(),
        ),
        "investors": pd.DataFrame(
            [[sessions[20], sessions[20], "TSEPrime", 1.0]],
            columns="PubDate EnDate Section FrgnBal".split(),
        ),
        "topix": pd.DataFrame({"Date": sessions, "C": np.linspace(100, 101, len(sessions))}),
    }


class FixedRules(unittest.TestCase):
    def five_signals(self):
        source = features(self.raw, self.sessions, self.cfg)
        base = source.loc[source.Code.eq("10000")].copy()
        # Three top-ranked names share S33; all five share S17. Reverse code order for the tie.
        return pd.concat(
            [
                base.assign(
                    Code=code,
                    S33=sector,
                    AvgTradingValue20=turnover * 10_000_000,
                    VolumeRatio=volume,
                    O=100.0,
                )
                for code, sector, turnover, volume in [
                    ("20000", "3650", 500, 2),
                    ("10000", "3650", 500, 1),
                    ("30000", "3650", 400, 9),
                    ("40000", "3600", 300, 8),
                    ("50000", "3600", 200, 7),
                ]
            ],
            ignore_index=True,
        )

    def setUp(self):
        self.sessions, self.raw = source()
        self.raw["bars"] = self.raw["bars"].astype(
            dict.fromkeys(["O", "H", "L", "C", "AdjFactor"], float)
        )
        self.cfg = Config(window=3, exit_window=2, max_hold=2, slots=1, capital=1_000_000)
        for df in self.raw.values():
            if "Code" in df:
                df["Code"] = df.Code.astype("string")

    def test_market_reform_uses_daily_membership_and_only_prior_publications(self):
        sessions = pd.bdate_range("2022-02-21", "2022-04-21").difference(
            pd.to_datetime(["2022-02-23", "2022-03-21"])
        )
        markets = [
            ("0101", "0111"),
            ("0102", "0112"),
            ("0104", "0113"),
            ("0106", "0112"),
            ("0107", "0113"),
            ("0105", "0105"),
            ("0109", "0109"),
            ("0111", "0101"),
            ("0101", None),
        ]
        codes = [f"{1000 + i}0" for i in range(len(markets))]
        daily = pd.MultiIndex.from_product([sessions, codes], names=["Date", "Code"]).to_frame(
            index=False
        )
        daily["Code"] = daily.Code.astype("string")
        master = daily.assign(ProdCat="011", S17="1", S33="3650")
        old, new = (master.Code.map(dict(zip(codes, seq))) for seq in zip(*markets))
        master["Mkt"] = old.where(master.Date.lt("2022-04-04"), new)
        # Exactly 500 million yen is admissible. The second security falls short by one yen.
        bars = daily.assign(
            O=100.0,
            H=101.0,
            L=99.0,
            C=100.0,
            Vo=5_000_000.0,
            Va=500_000_000.0,
            AdjFactor=1.0,
            ExRT=None,
        )
        bars.loc[bars.Code.eq(codes[1]), "Va"] -= 1
        margin = daily.rename(columns={"Date": "PubDate"}).assign(LongVol=1000)
        margin["Date"] = margin.PubDate - pd.offsets.BDay(1)
        filings = []
        for year in (2021, 2022):
            for code in codes:
                filings.append(
                    [
                        code,
                        pd.Timestamp(year, 2, 10),
                        "15:30:00",
                        "FYFinancialStatements",
                        "FY",
                        pd.Timestamp(year - 1, 1, 1),
                        pd.Timestamp(year - 1, 12, 31),
                        year,
                        year,
                    ]
                )
        fin = pd.DataFrame(
            filings,
            columns="Code DiscDate DiscTime DocType CurPerType CurPerSt CurPerEn Sales OP".split(),
        )
        flow = pd.DataFrame(
            [
                ("2022-03-31", "2022-03-25", "TSE1st", 1),
                ("2022-04-07", "2022-04-01", "TSE1st", 2),
                ("2022-04-07", "2022-04-01", "TSEPrime", 90),
                ("2022-04-14", "2022-04-01", "TSE1st", 99),
                ("2022-04-14", "2022-04-08", "TSEPrime", 3),
                ("2022-04-14", "2022-04-08", "TSEStandard", 88),
            ],
            columns=["PubDate", "EnDate", "Section", "FrgnBal"],
        )
        for col in ("PubDate", "EnDate"):
            flow[col] = pd.to_datetime(flow[col])
        raw = dict(
            bars=bars,
            master=master,
            financial=fin,
            investors=flow,
            margin=margin,
            topix=pd.DataFrame({"Date": sessions, "C": 100.0}),
        )
        result = features(raw, sessions, replace(self.cfg, exclude_pre_reform_growth=False))
        dates = pd.to_datetime(
            ["2022-04-01", "2022-04-04", "2022-04-07", "2022-04-08", "2022-04-14", "2022-04-15"]
        )
        stock = result.loc[result.Code.eq(codes[0])].set_index("Date").loc[dates]
        self.assertEqual(stock.FrgnBal.tolist(), [1, 1, 1, 2, 2, 3])
        self.assertTrue((stock.FlowDate < stock.index).all())
        self.assertTrue(stock.Eligible.all())
        self.assertFalse(result.loc[result.Code.eq(codes[1]), "Eligible"].any())
        for date, expected in [("2022-04-01", [0, 1, 2, 3, 4, 8]), ("2022-04-04", [0, 1, 3])]:
            actual = result.loc[result.Date.eq(date) & result.MarketUniverse, "Code"]
            self.assertEqual(set(actual), {codes[i] for i in expected})
        symmetric = features(raw, sessions, replace(self.cfg, exclude_pre_reform_growth=True))
        for date, expected in [("2022-04-01", [0, 1, 3, 8]), ("2022-04-04", [0, 1, 3])]:
            actual = symmetric.loc[symmetric.Date.eq(date) & symmetric.MarketUniverse, "Code"]
            self.assertEqual(set(actual), {codes[i] for i in expected})
        with self.assertRaisesRegex(ValueError, "Mkt"):
            features(dict(raw, master=master.drop(columns="Mkt")), sessions, self.cfg)

    def test_invalid_margin_never_enters_and_omitted_issue_expires(self):
        d = self.sessions[27]
        m = self.raw["margin"]
        m.loc[m.Date < d, "PubDate"] = m.loc[m.Date < d, "Date"]
        table = features(self.raw, self.sessions, self.cfg)
        self.assertFalse(table.loc[table.Date <= d, "MarginKnown"].any())
        self.raw["margin"] = m.loc[~(m.Code.eq("10000") & m.Date.eq(d))]
        table = features(self.raw, self.sessions, self.cfg)
        row = table.loc[table.Code.eq("10000") & table.Date.eq(self.sessions[29])].iloc[0]
        self.assertFalse(row.MarginKnown)

    def test_signal_and_future_invariance(self):
        table = features(self.raw, self.sessions, self.cfg)
        d = self.sessions[27]
        row = table.loc[table.Date.eq(d) & table.Code.eq("10000")].iloc[0]
        self.assertTrue(row.Signal)
        self.assertEqual(row.PriorHigh, 107)
        self.assertFalse(table.loc[table.Code.eq("30000"), "Eligible"].any())
        self.raw["bars"].loc[self.raw["bars"].Date > d, ["H", "C", "Vo", "AdjFactor"]] = [
            3,
            2,
            1,
            0.5,
        ]
        self.raw["margin"].loc[self.raw["margin"].PubDate >= d, "LongVol"] = 10000000
        revised = features(self.raw, self.sessions, self.cfg)
        pd.testing.assert_frame_equal(
            table.loc[table.Date <= d].reset_index(drop=True),
            revised.loc[revised.Date <= d].reset_index(drop=True),
        )

    def test_fixed_strict_high_inclusive_volume_margin_and_future_open(self):
        from trading_system.research.a0_5_rules.config import A0_5

        date = self.sessions[27]
        m = self.raw["bars"].Code.eq("10000")
        self.raw["bars"].loc[m & self.raw["bars"].Date.eq(date), "Vo"] = 15_000_000
        self.raw["margin"].loc[self.raw["margin"].Code.eq("10000"), "LongVol"] = 50_000_000

        def at_close():
            frame = features(self.raw, self.sessions, A0_5)
            return frame.loc[frame.Date.eq(date)]

        day = at_close()
        row = day.loc[day.Code.eq("10000")].iloc[0]
        assert row.Signal and row.VolumeRatio == 1.50 and row.MarginDays == 5
        first = orders(day, date, self.sessions, A0_5.capital, {}, A0_5)
        assert len(first) == 1
        self.raw["bars"].loc[m & self.raw["bars"].Date.eq(self.sessions[28]), "O"] = 1_000_000
        pd.testing.assert_frame_equal(
            first, orders(at_close(), date, self.sessions, A0_5.capital, {}, A0_5)
        )
        self.raw["bars"].loc[m & self.raw["bars"].Date.eq(date), "C"] = row.PriorHigh
        assert not at_close().loc[lambda d: d.Code.eq("10000"), "Signal"].item()

    def test_split_preserves_price_and_volume_comparison(self):
        d = self.sessions[27]
        m = self.raw["bars"].Code.eq("10000")
        normal = features(self.raw, self.sessions, self.cfg)
        b = self.raw["bars"]
        after = m & (b.Date >= d)
        b.loc[after, ["O", "H", "L", "C"]] = b.loc[after, ["O", "H", "L", "C"]].astype(float) * 0.5
        b.loc[after, "Vo"] *= 2
        b["AdjFactor"] = b.AdjFactor.astype(float)
        b.loc[m & b.Date.eq(d), ["AdjFactor", "ExRT"]] = [0.5, "1"]
        table = features(self.raw, self.sessions, self.cfg)
        key = table.Code.eq("10000") & table.Date.eq(d)
        self.assertEqual(
            table.loc[key, "PriorHigh"].item(), normal.loc[key, "PriorHigh"].item() / 2
        )
        self.assertEqual(table.loc[key, "VolumeRatio"].item(), 3)
        self.assertFalse(table.loc[key, "MarginKnown"].item())

    def test_pending_orders_cash_costs_and_screen_replay_parity(self):
        table = features(self.raw, self.sessions, self.cfg)
        cal = self.sessions.append(pd.DatetimeIndex([self.sessions[-1] + pd.DateOffset(days=1)]))
        signal = self.sessions[27]
        planned = orders(
            table.loc[table.Date.eq(signal)], signal, cal, self.cfg.capital, {}, self.cfg
        )
        result = simulate([(table, cal)], self.cfg)
        buy, sell = result.executions.to_dict("records")
        self.assertEqual(buy["shares"], planned.shares.item())
        self.assertEqual(buy["order_date"], self.sessions[28])
        self.assertEqual(sell["order_date"], self.sessions[30])
        self.assertGreater(buy["fee"], 0)
        self.assertAlmostEqual(
            self.cfg.capital + sell["realized_pnl"], result.equity.equity.iloc[-1]
        )
        self.assertAlmostEqual(result.metrics["gross_profit"], sell["realized_pnl"])
        self.assertGreaterEqual(result.equity.cash.min(), 0)
        segmented = simulate(
            [(table[table.Date <= signal], cal), (table[table.Date > signal], cal)], self.cfg
        )
        pd.testing.assert_frame_equal(result.equity, segmented.equity)

    def test_gap_rejection_and_unfilled_exit_retry(self):
        table = features(self.raw, self.sessions, self.cfg)
        d = self.sessions[27]
        plan = orders(table.loc[table.Date.eq(d)], d, self.sessions, self.cfg.capital, {}, self.cfg)
        log, positions = [], {}
        cash = execute(
            plan,
            {"10000": {"O": 1_000_000, "AdjFactor": 1}},
            self.sessions[28],
            self.cfg.capital,
            positions,
            self.cfg,
            log,
        )
        self.assertEqual(
            (cash, len(positions), log[0]["status"]), (self.cfg.capital, 0, "寄付資金不足")
        )
        positions["10000"] = Position(100, self.sessions[0], 10000, 100)
        plan = orders(table.loc[table.Date.eq(d)], d, self.sessions, 0, positions, self.cfg)
        execute(plan, {}, self.sessions[28], 0, positions, self.cfg, log)
        self.assertTrue(positions["10000"].pending_exit)
        retry = orders(
            table.loc[table.Date.eq(self.sessions[28])],
            self.sessions[28],
            self.sessions,
            0,
            positions,
            self.cfg,
        )
        got = execute(
            retry,
            {"10000": {"O": 50, "AdjFactor": 0.5, "ExRT": "1"}},
            self.sessions[29],
            0,
            positions,
            self.cfg,
            log,
        )
        self.assertEqual(log[-1]["filled_shares"], 200)
        self.assertAlmostEqual(got, 10000 * 0.999 * 0.9995)

    def test_holding_limit_exit_day_and_ma_only_exit(self):
        sessions = pd.bdate_range("2020-01-06", periods=41)
        day = features(self.raw, self.sessions, self.cfg)
        row = day.loc[day.Date.eq(self.sessions[27]) & day.Code.eq("10000")].copy()
        daily = [
            row.assign(
                Date=date,
                Signal=(i == 0),
                O=100.0,
                C=90.0 if i == 34 else 100.0,
                ExitMA=95.0,
                AdjFactor=1.0,
            )
            for i, date in enumerate(sessions[:-1])
        ]
        table = pd.concat(daily, ignore_index=True)
        for limit, duration, reason in [
            (15, 15, "保有期限"),
            (30, 30, "保有期限"),
            (None, 34, "終値が移動平均割れ"),
            (0, 34, "終値が移動平均割れ"),
        ]:
            with self.subTest(limit=limit):
                result = simulate([(table, sessions)], replace(self.cfg, max_hold=limit))
                sell = result.executions.loc[result.executions.side.eq("SELL")].iloc[0]
                self.assertEqual((sell.reason, sell.order_date), (reason, sessions[1 + duration]))
                self.assertEqual(result.metrics["average_holding_days"], duration)
        for invalid in [-1, 1.4, False]:
            with self.assertRaises(ValueError):
                replace(self.cfg, max_hold=invalid)

    def test_daily_cap_fills_two_most_liquid_and_ties_by_code(self):
        table = self.five_signals()
        calendar = self.sessions.append(pd.DatetimeIndex([self.sessions[-1] + pd.offsets.BDay(1)]))
        cfg = replace(self.cfg, slots=5, max_daily_entries=2, max_positions_per_sector=None)
        result = simulate([(table, calendar)], cfg)
        buys = result.executions.query("side == 'BUY' and status == '約定'")
        self.assertEqual(buys.Code.tolist(), ["10000", "20000"])
        self.assertEqual(result.metrics["max_daily_entries_filled"], 2)
        self.assertEqual(result.equity.signals.max(), 5)

    def test_sector_cap_counts_existing_and_admitted_even_with_planned_exit(self):
        table = self.five_signals()
        date = self.sessions[27]
        calendar = self.sessions.append(pd.DatetimeIndex([self.sessions[-1] + pd.offsets.BDay(1)]))
        cfg = replace(self.cfg, slots=5, max_daily_entries=0, max_positions_per_sector=2)
        result = simulate([(table, calendar)], cfg)
        buys = result.executions.query("side == 'BUY' and status == '約定'")
        self.assertEqual(buys.Code.tolist(), ["10000", "20000", "40000", "50000"])
        self.assertEqual(result.metrics["max_sector_holdings"], 2)
        day = table.loc[table.Date.eq(date)]
        held = {"30000": Position(100, calendar[0], 10000, 100, sector33="3600")}
        plan = orders(day, date, calendar, cfg.capital, held, cfg)
        self.assertEqual(plan.loc[plan.side.eq("SELL"), "Code"].tolist(), ["30000"])
        self.assertEqual(
            plan.loc[plan.side.eq("BUY"), "Code"].tolist(), ["10000", "40000", "50000"]
        )
        free = orders(
            day,
            date,
            calendar,
            cfg.capital,
            {},
            replace(cfg, max_positions_per_sector=0, max_daily_entries=None),
        )
        self.assertEqual(len(free), 5)


CAL = pd.bdate_range("2026-10-15", "2026-11-27").difference(
    pd.to_datetime(["2026-11-03", "2026-11-23"])
)
DATE = pd.Timestamp("2026-11-13")
NOW = "2026-11-13 17:00+09:00"


def bars():
    return pd.DataFrame(
        dict(
            Code=["10000", "20000", "30000", "40000"],
            CoName=list("ABCD"),
            S33=["0050", "0050", "1050", "2050"],
            C=1000.0,
            ExitMA=1000.0,
            Signal=True,
            Listed=True,
            AvgTradingValue20=[4e9, 3e9, 2e9, 1e9],
            Date=pd.array([DATE] * 4, dtype="datetime64[ns]"),
            MarketUniverse=True,
            MarginKnown=True,
            Eligible=True,
            is_published_date_estimated=False,
            AdjFactor=1.0,
            FinKnown=True,
            FlowKnown=True,
            PriorHigh=100.0,
            VolumeRatio=1.0,
            SectorReturn=0.0,
            SectorShare=0.01,
            PriorShare=0.01,
            IndexReturn=0.0,
        )
    )


def manual(day):
    return {c: ("", "手動確認") for c in day.Code}


def test_three_sessions_holiday_no_promotion_and_restricted_cash():
    day = bars()
    state = dict(
        initial(), cash=1_000_000.0, settlements=[dict(date="2026-11-18", amount=849_000.0)]
    )
    schedule = manual(day)
    schedule["10000"] = ("2026-11-18", "公式")
    orders, notes, s = plan(day, DATE, CAL, state, schedule, {})
    assert orders.empty and notes["10000"]["decision"] == "決算3営業日以内見送り"
    assert s["available"] == 151_000 and s["free_slots"] == 5
    assert not any(x["decision"] == "寄成買い" for x in notes.values())
    schedule["10000"] = ("2026-11-19", "公式")
    orders, _, _ = plan(day, DATE, CAL, state, schedule, {})
    assert list(orders.shares) == [100]
    assert buying_power(state, "2026-11-18") == 1_000_000.0
    state["cash"] = 5_000_000
    schedule["10000"] = ("2026-11-16", "公式")
    orders, _, _ = plan(day, DATE, CAL, state, schedule, {})
    assert list(orders.Code) == ["20000"]  # Two reserved slots, one removed.
    day.Date = pd.Timestamp("2026-11-19")
    schedule["10000"] = ("2026-11-25", "公式")
    _, notes, _ = plan(day, day.Date.iloc[0], CAL, state, schedule, {})
    assert notes["10000"]["decision"] == "決算3営業日以内見送り"  # Nov 20, 24, 25


def test_sell_priority_frees_capacity_but_does_not_spend_proceeds():
    day = bars()
    day.loc[:1, "ExitMA"] = 1100.0
    positions = [
        dict(Code=r.Code, shares=100, entry_date="2026-10-16", S33=r.S33)
        for r in day.iloc[:3].itertuples()
    ]
    state = dict(initial(), cash=0.0, positions=positions)
    schedule = manual(day)
    schedule["10000"] = ("2026-11-16", "公式")
    orders, notes, meta = plan(day, DATE, CAL, state, schedule, {"20000": "分割"})
    assert list(orders.reason) == ["EARNINGS_AVOID", "SMA10_BREAK", "MAX_HOLD_15D"]
    assert meta["free_slots"] == 5 and meta["available"] == 0 and meta["sector_free"]["0050"] == 2
    assert "10日線" in meta["alerts"][0]
    state["cash"] = 5e6
    # A third same-sector buy can only be made after the two sector exits.
    day.loc[3, "S33"] = "0050"
    orders, _, _ = plan(day, DATE, CAL, state, schedule, {})
    assert orders.iloc[-1].side == "BUY" and orders.iloc[-1].conditional_on_sales


def test_guard_blocks_stale_small_coverage_and_relative_margin_drop():
    row = bars().iloc[[0]]
    frame = row.loc[row.index.repeat(3200)].reset_index(drop=True)
    frame["Code"] = [str(i) for i in range(len(frame))]
    assert freshness(frame, DATE, CAL, NOW)["eligible"] == 3200
    assert freshness(frame.iloc[:2500].assign(Eligible=False), DATE, CAL, NOW)["data_ready"] == 2500
    with pytest.raises(ValueError, match="2,500"):
        freshness(frame.iloc[:2499], DATE, CAL, NOW)
    small = frame.iloc[:2600].copy()
    small.loc[2300:, "MarginKnown"] = False
    prior = frame.copy()
    prior["Date"] = pd.array(["2026-11-12"] * len(prior), dtype="datetime64[ns]")
    with pytest.raises(ValueError, match="有効信用残"):
        freshness(pd.concat([prior, small]), DATE, CAL, NOW)
    with pytest.raises(ValueError, match="直近"):
        freshness(frame, DATE, CAL, "2026-11-16 17:00+09:00")
    with pytest.raises(ValueError, match="カレンダー"):
        latest_close(CAL, "2027-01-05")
    assert latest_close(CAL, "2026-11-16 08:00+09:00") == DATE
    with pytest.raises(ValueError, match="寄付"):
        latest_close(CAL, "2026-11-16 09:01+09:00")
