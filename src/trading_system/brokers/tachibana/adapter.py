"""tachibana: transport intentionally absent until a later authorized phase."""

from trading_system.brokers.base import DisconnectedAdapter


class Adapter(DisconnectedAdapter):
    broker = "tachibana"
