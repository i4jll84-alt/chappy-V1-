"""Vendor documentation and this implementation's availability are separate."""

from enum import StrEnum

from trading_system.backtest.models import Input


class Feature(StrEnum):
    CONNECT = "connect"
    CONNECTION = "connection_status"
    QUOTE = "quote"
    BOARD = "board"
    CASH_BUY = "cash_buy"
    CASH_SELL = "cash_sell"
    MARGIN_SHORT = "margin_short"
    MARGIN_COVER = "margin_cover"
    ORDERS = "orders"
    CANCEL = "cancel"
    MODIFY = "modify"
    FILLS = "fills"
    POSITIONS = "positions"
    BALANCE = "balance"
    SHORT_AVAILABILITY = "short_availability"
    COSTS = "costs"


class Capability(Input):
    vendor_support: str
    implementation: str
    verification: str
    fallback: str = "none"
    reason: str
    source: str | None = None


SOURCES = {
    "rakuten": "https://marketspeed.jp/ms2_rss/onlinehelp/ohm_002/ohm_002_06.html",
    "tachibana": "https://www.e-shiten.jp/api/",
    "mufg_esmart": "https://kabucom.github.io/kabusapi/reference/",
}


def capabilities(broker, *, paper=False):
    result = {}
    for feature in Feature:
        documented = feature in {
            Feature.CASH_BUY,
            Feature.CASH_SELL,
            Feature.MARGIN_SHORT,
            Feature.MARGIN_COVER,
        }
        # Do not infer an API's limitations from a missing local implementation.
        result[feature] = Capability(
            vendor_support="documented_family" if documented else "unverified",
            implementation="not_implemented",
            verification="not_live_verified",
            reason="Phase 1 has no transport; detailed constraints still require verification",
            source=SOURCES[broker],
        )
    if paper:
        for feature in Feature:
            result[feature] = Capability(
                vendor_support="not_applicable",
                implementation="unsupported" if feature == Feature.MODIFY else "simulated",
                verification="offline_tests_only",
                reason=(
                    "Atomic amendment unavailable; no automatic cancel/replace fallback"
                    if feature == Feature.MODIFY
                    else "Explicit local simulation only"
                ),
            )
    return result
