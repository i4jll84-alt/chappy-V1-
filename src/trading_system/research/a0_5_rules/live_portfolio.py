import math
import re

import pandas as pd

from .data_fetcher import iso


def number(value, *, positive=False):
    try:
        x = float(value)
    except (ValueError, TypeError):
        raise ValueError("数値を入力してください。") from None
    if not math.isfinite(x) or x < 0 or (positive and not x > 0):
        raise ValueError("金額・単価・株数を確認してください。")
    return x


def code_of(value):
    value = str(value).strip().upper()
    if not re.fullmatch("[0-9A-Z]{4}0", value):
        raise ValueError("銘柄コードは国内株の5桁コードで入力してください。")
    return value


def quantity(value):
    x = number(value, positive=True)
    if not x.is_integer() or x % 100:
        raise ValueError("株数は正の100株単位で入力してください。")
    return int(x)


def daystr(value):
    if pd.isna(value) or not str(value).strip():
        raise ValueError("日付を入力してください。")
    return iso(value)


def buying_power(state, date):
    locked = sum(item["amount"] for item in state["settlements"] if item["date"] > daystr(date))
    return round(state["cash"] - locked, 6)
