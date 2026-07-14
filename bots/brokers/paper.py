"""Paper (simulated) broker -- the default everywhere in this repo.

Tracks cash and positions in a local JSON file and fills orders at the price
you feed it (live quote via yfinance when available, or a price you pass in
for offline testing). No real money can move through this class.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Optional

from bots.brokers.base import Broker, OrderResult

DEFAULT_STATE_PATH = os.path.join("bot_data", "paper_account.json")


class PaperBroker(Broker):
    name = "paper"
    is_paper = True

    def __init__(
        self,
        starting_cash: float = 10_000.0,
        state_path: str = DEFAULT_STATE_PATH,
        price_overrides: Optional[Dict[str, float]] = None,
    ):
        self.state_path = state_path
        self.price_overrides = price_overrides or {}
        self._cash = starting_cash
        self._positions: Dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.state_path):
            return
        with open(self.state_path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        self._cash = state.get("cash", self._cash)
        self._positions = state.get("positions", {})

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump({"cash": self._cash, "positions": self._positions}, fh, indent=2)

    # -- Broker interface -----------------------------------------------------

    def cash(self) -> float:
        return self._cash

    def positions(self) -> Dict[str, float]:
        return {s: q for s, q in self._positions.items() if q}

    def price(self, symbol: str) -> float:
        symbol = symbol.upper()
        if symbol in self.price_overrides:
            return self.price_overrides[symbol]
        import yfinance as yf

        data = yf.Ticker(symbol).history(period="1d")
        if data.empty:
            raise ValueError(f"No price data for {symbol}")
        return float(data["Close"].iloc[-1])

    def buy(self, symbol: str, quantity: float) -> OrderResult:
        symbol = symbol.upper()
        try:
            fill = self.price(symbol)
        except Exception as exc:
            return OrderResult(False, symbol, "buy", quantity, error=str(exc))
        cost = fill * quantity
        if cost > self._cash:
            return OrderResult(
                False, symbol, "buy", quantity,
                error=f"insufficient cash: need {cost:.2f}, have {self._cash:.2f}",
            )
        self._cash -= cost
        self._positions[symbol] = self._positions.get(symbol, 0.0) + quantity
        self._save()
        return OrderResult(True, symbol, "buy", quantity, fill_price=fill, order_id="paper")

    def sell(self, symbol: str, quantity: float) -> OrderResult:
        symbol = symbol.upper()
        held = self._positions.get(symbol, 0.0)
        if quantity > held:
            return OrderResult(
                False, symbol, "sell", quantity, error=f"only hold {held} shares"
            )
        try:
            fill = self.price(symbol)
        except Exception as exc:
            return OrderResult(False, symbol, "sell", quantity, error=str(exc))
        self._cash += fill * quantity
        self._positions[symbol] = held - quantity
        if not self._positions[symbol]:
            del self._positions[symbol]
        self._save()
        return OrderResult(True, symbol, "sell", quantity, fill_price=fill, order_id="paper")
