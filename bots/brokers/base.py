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
