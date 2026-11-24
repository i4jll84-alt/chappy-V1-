"""Flag contradictory empty RSS observations without replacing source versions."""

EMPTY_REGRESSION = "empty_after_completed_trade"


def initialize_quality(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS bar_quality_events(
            symbol TEXT, bar_time TEXT, empty_frame INTEGER, priced_frame INTEGER,
            detected_frame INTEGER, reason TEXT,
            PRIMARY KEY(symbol,bar_time,empty_frame))
    """)


def record_empty_regression(db, symbol, bar_time, empty_frame, detected_frame):
    """Keep the first detection time; a prior priced bar is evidence, not a fill."""
    existing = db.execute(
        "SELECT 1 FROM bar_quality_events WHERE symbol=? AND bar_time=? AND empty_frame=?",
        (symbol, bar_time, empty_frame),
    ).fetchone()
    if existing:
        return True
    prior = db.execute(
        "SELECT frame FROM bars WHERE symbol=? AND bar_time=? AND frame<? "
        "AND elapsed_minute=1 AND o IS NOT NULL AND volume>0 ORDER BY frame DESC LIMIT 1",
        (symbol, bar_time, empty_frame),
    ).fetchone()
    if prior is None:
        return False
    db.execute(
        "INSERT INTO bar_quality_events VALUES(?,?,?,?,?,?)",
        (symbol, bar_time, empty_frame, prior[0], detected_frame, EMPTY_REGRESSION),
    )
    return True


def unresolved_counts(db, local_day):
    """Only the latest source version can be unresolved; never select an older price."""
    return db.execute(
        "SELECT count(*),coalesce(sum(substr(e.bar_time,1,10)=?),0) "
        "FROM bar_quality_events e WHERE e.empty_frame=("
        "SELECT max(b.frame) FROM bars b WHERE b.symbol=e.symbol AND b.bar_time=e.bar_time)",
        (local_day,),
    ).fetchone()
