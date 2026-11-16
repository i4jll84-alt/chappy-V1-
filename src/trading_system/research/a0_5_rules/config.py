from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RESULTS = ROOT / "results"

FINANCIAL_HISTORY_DAYS = 560
MARGIN_BALANCE_FALLBACK_OFFSET_BDAYS = 3
MIN_AVG_TRADING_VALUE_20 = 500_000_000
MAX_DAILY_ENTRIES: int = 2
MAX_POSITIONS_PER_SECTOR: int = 2
EXCLUDE_PRE_REFORM_GROWTH: bool = True
MAX_HOLDING_DAYS: int | None = 15
REQUIRE_TOPIX_ABOVE_SMA75: bool = False
REQUIRE_FOREIGNERS_NET_BUY: bool = False
LONG_TREND_FILTER = "NONE"
LONG_TREND_MODES = ("NONE", "ABOVE_SMA200", "SMA50_ABOVE_SMA200")
# Default sync keeps enough price history for the configured screener.
PRICE_HISTORY_DAYS = max(
    115, 150 if REQUIRE_TOPIX_ABOVE_SMA75 else 0, 365 if LONG_TREND_FILTER != "NONE" else 0
)
USE_ATR_RISK_MANAGEMENT: bool = False
ATR_PERIOD: int = 14
ATR_STOP_MULTIPLIER: float = 2.0
RISK_PER_TRADE_PCT: float = 0.01
MAX_POSITION_WEIGHT_PCT: float = 0.20


@dataclass(frozen=True)
class Config:
    window: int = 20
    exit_window: int = 10
    volume_multiple: float = 1.5
    margin_days: float = 5
    max_hold: int | None = MAX_HOLDING_DAYS
    slots: int = 5
    capital: int = 5_000_000
    fee_bps: float = 5
    slip_bps: float = 10
    lot: int = 100
    max_daily_entries: int | None = MAX_DAILY_ENTRIES
    max_positions_per_sector: int | None = MAX_POSITIONS_PER_SECTOR
    exclude_pre_reform_growth: bool = EXCLUDE_PRE_REFORM_GROWTH
    use_atr_risk_management: bool = USE_ATR_RISK_MANAGEMENT
    long_trend_filter: str = LONG_TREND_FILTER
    require_topix_above_sma75: bool = REQUIRE_TOPIX_ABOVE_SMA75
    require_foreigners_net_buy: bool = REQUIRE_FOREIGNERS_NET_BUY

    def __post_init__(self):
        if self.max_hold is not None and (type(self.max_hold) is not int or self.max_hold < 0):
            raise ValueError("保有期限は0以上の整数またはNoneで指定してください。")
        if self.long_trend_filter not in LONG_TREND_MODES:
            raise ValueError(f"long_trend_filter must be one of {LONG_TREND_MODES}")
        for cap in (self.max_daily_entries, self.max_positions_per_sector):
            if cap is not None and (type(cap) is not int or cap < 0):
                raise ValueError("分散上限は0以上の整数またはNoneで指定してください。")
        if not (2 <= self.window <= 60 and 2 <= self.exit_window <= 60):
            raise ValueError("移動窓は2～60営業日で指定してください。")
        if not all(
            isinstance(v, int) and v > 0
            for v in (self.window, self.exit_window, self.slots, self.lot)
        ):
            raise ValueError("日数・保有枠・売買単位には正の整数が必要です。")
        if not all(
            isfinite(v) and v > 0 for v in (self.capital, self.margin_days, self.volume_multiple)
        ):
            raise ValueError("資金・信用残日数・出来高倍率は正の数で指定してください。")
        if not all(0 <= v < 10000 for v in (self.fee_bps, self.slip_bps)):
            raise ValueError("片道コストは0～10000未満のbpで指定してください。")


# Pin the daily buy mandate independently of the defaults used to construct research cases.
A0_5: Final[Config] = Config(
    window=20,
    exit_window=10,
    volume_multiple=1.50,
    margin_days=5,
    max_hold=15,
    slots=5,
    max_daily_entries=2,
    max_positions_per_sector=2,
    capital=5_000_000,
    fee_bps=5,
    slip_bps=10,
    lot=100,
    exclude_pre_reform_growth=True,
    use_atr_risk_management=False,
    long_trend_filter="NONE",
    require_topix_above_sma75=False,
    require_foreigners_net_buy=False,
)
