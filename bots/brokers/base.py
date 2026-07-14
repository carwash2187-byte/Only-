"""Common broker interface. Every connector implements the same five calls so
the trading desk can switch between paper, Alpaca, Robinhood, and crypto
exchanges without changing strategy code."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class OrderResult:
    ok: bool
    symbol: str
    side: str  # "buy" or "sell"
    quantity: float
    fill_price: Optional[float] = None
    order_id: Optional[str] = None
    error: Optional[str] = None


class Broker(ABC):
    name: str = "abstract"
    is_paper: bool = True

    @abstractmethod
    def cash(self) -> float:
        """Buying power / settled cash in the account currency."""

    @abstractmethod
    def positions(self) -> Dict[str, float]:
        """Open positions as {symbol: quantity}."""

    @abstractmethod
    def price(self, symbol: str) -> float:
        """Latest trade price for symbol."""

    @abstractmethod
    def buy(self, symbol: str, quantity: float) -> OrderResult:
        """Market buy."""

    @abstractmethod
    def sell(self, symbol: str, quantity: float) -> OrderResult:
        """Market sell."""

    def equity(self) -> float:
        total = self.cash()
        for symbol, quantity in self.positions().items():
            try:
                total += quantity * self.price(symbol)
            except Exception:
                pass
        return total

    def has_pending_order(self, symbol: str) -> bool:
        """True if symbol has an order placed but not yet filled.

        Matters for brokers that queue orders instead of filling them
        instantly (e.g. a stock broker outside market hours): without this
        check, running the desk twice before the first order fills submits a
        second, duplicate order for the same symbol. Default False is
        correct for brokers/venues that fill immediately (paper, crypto).
        """
        return False
