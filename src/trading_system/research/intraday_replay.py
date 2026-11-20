"""Observation helpers retained for archived studies; fixed-rule trading was retired."""

from datetime import UTC, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
SESSIONS = (("09:00", "10:00", "10:30"), ("12:30", "13:30", "14:00"))


def stamp(day, clock):
    return datetime.fromisoformat(day + "T" + clock).replace(tzinfo=JST).astimezone(UTC)


def fmt(t):
    return t.astimezone(UTC).isoformat(timespec="microseconds")


def read_signals(db, codes, day, session):
    start, decision, target = [stamp(day, t) for t in session]
    # Pick the last received version BEFORE inspecting quality, including revisions to NULL.
    sql = """
        WITH versions AS (
          SELECT *,ROW_NUMBER() OVER(PARTITION BY symbol,bar_at
                     ORDER BY known_at DESC,source_id DESC) AS revision
          FROM minute_bars WHERE source='rakuten' AND known_at<=?
            AND (bar_at=? OR (bar_at>=? AND bar_at<?))
        )
        SELECT * FROM versions WHERE revision=1 AND elapsed_minute=1
          AND volume>0 AND o>0 AND c>0 ORDER BY bar_at
    """
    latest, opening = {}, {}
    for b in db.execute(
        sql, (fmt(decision), fmt(start), fmt(decision - timedelta(minutes=5)), fmt(decision))
    ):
        assert (
            datetime.fromisoformat(b["bar_at"]) + timedelta(minutes=1)
            <= datetime.fromisoformat(b["known_at"])
            <= decision
        )
        if b["bar_at"] == fmt(start):
            opening[b["symbol"]] = b
        else:
            latest[b["symbol"]] = b
    signals = []
    for code in codes:
        o, c = opening.get(code), latest.get(code)
        signals.append(
            dict(
                day=day,
                symbol=code,
                decision_at=fmt(decision),
                exit_target=fmt(target),
                opening=o["o"] if o else None,
                opening_received=o["known_at"] if o else None,
                recent_close=c["c"] if c else None,
                recent_bar=c["bar_at"] if c else None,
                recent_received=c["known_at"] if c else None,
            )
        )
    return signals


def quotes(db, day):
    rows = db.execute(
        """SELECT * FROM quotes WHERE source='rakuten' AND source_quality='observed_book'
        AND known_at>=? AND known_at<? ORDER BY known_at,symbol,source_id""",
        (fmt(stamp(day, "09:00")), fmt(stamp(day, "15:25"))),
    )
    keep = []
    for q in rows:
        at = datetime.fromisoformat(q["known_at"])
        local = at.astimezone(JST).strftime("%H:%M")
        if "11:30" <= local < "12:30":
            continue
        start = stamp(day, "09:00" if local < "11:30" else "12:30")
        if datetime.fromisoformat(q["read_started_at"]) < start:
            continue
        if min(datetime.fromisoformat(q[k]) for k in ("bid_at", "ask_at")) < start:
            continue
        assert 0 < q["bid"] <= q["ask"] and min(q["bid_qty"], q["ask_qty"]) > 0
        assert all(
            0 <= (at - datetime.fromisoformat(q[k])).total_seconds() <= 60
            for k in ("bid_at", "ask_at")
        )
        keep.append(q)
    return keep
