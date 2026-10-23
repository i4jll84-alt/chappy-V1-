"""Validate abbreviated J-Quants bars without changing the source row."""

from __future__ import annotations

import math
import re
from datetime import date

FIELDS = (
    "Date",
    "Code",
    "O",
    "H",
    "L",
    "C",
    "Vo",
    "Va",
    "AdjFactor",
    "AdjO",
    "AdjH",
    "AdjL",
    "AdjC",
    "AdjVo",
)
RAW = ("O", "H", "L", "C", "Vo", "Va")
PAIRS = (("O", "AdjO"), ("H", "AdjH"), ("L", "AdjL"), ("C", "AdjC"), ("Vo", "AdjVo"))
CODE = re.compile(r"[0-9][0-9A-Z]{3,4}", re.ASCII)


def _canonical(code):
    if not isinstance(code, str) or CODE.fullmatch(code) is None:
        return None
    return code + "0" if len(code) == 4 else code


def validate_price(row: dict, expected_code: str | None = None) -> list[dict]:
    issues = []

    def add(rule, field, severity="error"):
        issues.append({"severity": severity, "rule": rule, "field": field})

    for field in FIELDS:
        if field not in row:
            add("missing_field", field)
    if "Date" in row:
        value = row["Date"]
        try:
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError:
            add("invalid_date", "Date")
    if "Code" in row:
        canonical = _canonical(row["Code"])
        if canonical is None:
            add("invalid_code", "Code")
        if expected_code is not None:
            expected = _canonical(expected_code)
            if expected is None:
                add("invalid_expected_code", "Code")
            elif canonical is not None and canonical != expected:
                add("code_mismatch", "Code")

    present = [row[field] for field in RAW if field in row]
    no_trades = len(present) == len(RAW) and all(value is None for value in present)
    if no_trades:
        add("no_trades", "O,H,L,C,Vo,Va", "info")
    elif any(value is None for value in present):
        for field in RAW:
            if field in row and row[field] is None:
                add("partial_null", field)

    valid = {}
    for field in FIELDS[2:]:
        if field not in row:
            continue
        value = row[field]
        if value is None:
            if field == "AdjFactor":
                add("invalid_number", field)
            continue
        try:
            positive = (
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value)
                and value > 0
            )
        except (OverflowError, TypeError):
            positive = False
        if not positive:
            add("invalid_number", field)
        else:
            valid[field] = value
    for raw, adjusted in PAIRS:
        if raw in row and adjusted in row and (row[raw] is None) != (row[adjusted] is None):
            add("null_parity", adjusted)
    for prefix in ("", "Adj"):
        o, h, low, c = (prefix + suffix for suffix in ("O", "H", "L", "C"))
        if all(field in valid for field in (o, h, low, c)):
            if not (valid[low] <= valid[o] <= valid[h] and valid[low] <= valid[c] <= valid[h]):
                add("ohlc_order", h)
    return issues
