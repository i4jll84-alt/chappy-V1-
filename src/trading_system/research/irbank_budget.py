"""Separate an explicitly renewed trial budget from the preserved lifetime request ledger."""

import hashlib
import json
from datetime import date, datetime


def prefix_proof(db, initial, start):
    rows = db.execute(
        "SELECT id,day,started,code,end_date,http_status FROM requests "
        "WHERE kind='data' AND day<? ORDER BY id",
        (start,),
    ).fetchall()
    body = json.dumps(
        {"initial_data_requests": initial, "rows": rows}, ensure_ascii=False, separators=(",", ":")
    ).encode()
    return {"data_attempts": initial + len(rows), "sha256": hashlib.sha256(body).hexdigest()}


def data_totals(policy, db):
    initial = policy["initial_data_requests"]
    lifetime = initial + db.execute("SELECT count(*) FROM requests WHERE kind='data'").fetchone()[0]
    renewed = policy.get("renewed_trial_budget")
    if renewed is None:
        return {"lifetime": lifetime, "budget": lifetime, "prior": 0, "start": policy["starts_at"]}
    start = renewed["starts_on"]
    begin = date.fromisoformat(start)
    original = datetime.fromisoformat(policy["starts_at"]).date()
    end = datetime.fromisoformat(policy["ends_before"]).date()
    if not original < begin < end or not 1 <= (end - begin).days <= 10:
        raise ValueError("invalid_renewed_trial_period")
    proof = prefix_proof(db, initial, start)
    if proof != renewed["preserved_prior_ledger"]:
        raise ValueError("renewed_trial_prior_ledger_changed")
    if (
        not renewed.get("authorized_at")
        or renewed.get("max_requests") != policy["max_total_requests"]
    ):
        raise ValueError("renewed_trial_budget_not_authorized")
    return {
        "lifetime": lifetime,
        "budget": lifetime - proof["data_attempts"],
        "prior": proof["data_attempts"],
        "start": start,
    }
