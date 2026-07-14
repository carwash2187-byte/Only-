"""Broker connectors. Paper trading is the default; live brokers require your
own API keys and an explicit choice."""

from __future__ import annotations

from bots.brokers.base import Broker, OrderResult
from bots.brokers.paper import PaperBroker

__all__ = ["Broker", "OrderResult", "PaperBroker", "get_broker"]


def get_broker(name: str = "paper", **kwargs) -> Broker:
    """Factory: 'paper' (default), 'alpaca', 'robinhood', or 'crypto'."""
    name = name.lower()
    if name == "paper":
        return PaperBroker(**kwargs)
    if name == "alpaca":
        from bots.brokers.alpaca import AlpacaBroker

        return AlpacaBroker(**kwargs)
    if name == "robinhood":
        from bots.brokers.robinhood import RobinhoodBroker

        return RobinhoodBroker(**kwargs)
    if name == "crypto":
        from bots.brokers.crypto import CryptoBroker

        return CryptoBroker(**kwargs)
    if name == "oanda":
        from bots.brokers.oanda import OandaBroker

        return OandaBroker(**kwargs)
    raise ValueError(
        f"Unknown broker '{name}'. Options: paper, alpaca, robinhood, crypto, oanda"
    )
