"""Validate authorized archival windows without resetting the lifetime request ledger."""

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
    total_ceiling(policy)
    initial = policy["initial_data_requests"]
    lifetime = initial + db.execute("SELECT count(*) FROM requests WHERE kind='data'").fetchone()[0]
    renewed = policy.get("renewed_trial_budget")
    if renewed is None:
        return {"lifetime": lifetime, "budget": lifetime, "prior": 0, "start": policy["starts_at"]}
    start = renewed["starts_on"]
    begin = date.fromisoformat(start)
    original = datetime.fromisoformat(policy["starts_at"]).date()
    end = datetime.fromisoformat(renewed.get("ends_before", policy["ends_before"])).date()
    if not original < begin < end or not 1 <= (end - begin).days <= 10:
        raise ValueError("invalid_renewed_trial_period")
    proof = prefix_proof(db, initial, start)
    if proof != renewed["preserved_prior_ledger"]:
        raise ValueError("renewed_trial_prior_ledger_changed")
    if not renewed.get("authorized_at") or renewed.get("max_requests") != policy.get(
        "paid_extension", {}
    ).get("prior_max_total_requests", policy["max_total_requests"]):
        raise ValueError("renewed_trial_budget_not_authorized")
    return {
        "lifetime": lifetime,
        "budget": lifetime - proof["data_attempts"],
        "prior": proof["data_attempts"],
        "start": start,
    }


def total_ceiling(policy):
    """Only an explicit paid extension may lift the legacy ten-day ceiling."""
    extension = policy.get("paid_extension")
    if extension is None:
        renewal_end = policy.get("renewed_trial_budget", {}).get("ends_before")
        if renewal_end is not None and renewal_end != policy["ends_before"]:
            raise ValueError("paid_extension_authorization_required")
        return 10000 if policy.get("quota_accounting") == "data_only" else 9500
    renewed = policy.get("renewed_trial_budget", {})
    schedule = policy["daily_schedule"]
    start = date.fromisoformat(renewed["starts_on"])
    end = datetime.fromisoformat(policy["ends_before"])
    old_end = datetime.fromisoformat(renewed["ends_before"])
    authorized = datetime.fromisoformat(extension["authorized_at"])
    ceiling = (end.date() - start).days * 1000
    if (
        policy.get("quota_accounting") != "data_only"
        or extension.get("authorized") is not True
        or not extension.get("authorization")
        or authorized.utcoffset() is None
        or end.utcoffset() is None
        or old_end.utcoffset() is None
        or authorized >= end
        or end <= old_end
        or end.hour != 0
        or end.minute != 0
        or end.second != 0
        or end.microsecond != 0
        or extension.get("ends_before") != policy["ends_before"]
        or extension.get("prior_max_total_requests") != renewed.get("max_requests")
        or schedule["from"] != renewed["starts_on"]
        or date.fromisoformat(schedule["through"]).toordinal() + 1 != end.date().toordinal()
        or extension.get("max_total_requests") != ceiling
        or policy["max_total_requests"] != ceiling
    ):
        raise ValueError("invalid_paid_extension")
    return ceiling
