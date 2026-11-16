import json
from datetime import timedelta, timezone

import pandas as pd

JST = timezone(timedelta(hours=9))
SCHEMA = {
    "bars": ("/equities/bars/daily", "Date Code O H L C Vo Va AdjFactor ExRT UL LL"),
    "master": ("/equities/master", "Date Code CoName S17 S33 ProdCat Mkt MktNm"),
    "topix": ("/indices/bars/daily/topix", "Date C"),
    "financial": (
        "/fins/summary",
        "DiscDate DiscTime Code DiscNo DocType CurPerType CurPerSt CurPerEn Sales OP",
    ),
    "investors": ("/equities/investor-types", "PubDate EnDate Section FrgnBal"),
    "margin": ("/markets/margin-interest", "Date PubDate Code LongVol"),
}
NUMERIC = set("O H L C Vo Va AdjFactor Sales OP FrgnBal LongVol".split())


def iso(value):
    return pd.Timestamp(value).date().isoformat()


def read_parts(path, name, start, end, *, columns=None):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    parts = [
        pd.read_parquet(path.parents[1] / part["file"], columns=columns)
        for part in manifest["files"][name]
        if part["first"] <= iso(end) and part["last"] >= iso(start)
    ]
    cols = SCHEMA[name][1].split()
    if not parts:
        return pd.DataFrame(columns=columns if columns is not None else cols)
    if name == "master" and any("Mkt" not in part for part in parts):
        raise ValueError(
            "市場区分 Mkt のない旧版が含まれます。市場区分付きスナップショットを選んでください。"
        )
    df = pd.concat(parts, ignore_index=True).drop_duplicates()
    day = cols[0]
    for col in set(["Date", "DiscDate", "PubDate", "EnDate", "CurPerSt", "CurPerEn"]).intersection(
        df
    ):
        if (
            name == "margin"
            and col == "PubDate"
            and not pd.api.types.is_datetime64_any_dtype(df[col])
        ):
            df[col] = df[col].str.strip().replace("", pd.NA)
        df[col] = pd.to_datetime(df[col], format="ISO8601").astype("datetime64[ns]")
    return df.loc[df[day].between(pd.Timestamp(start), pd.Timestamp(end))].copy()
