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
from bots.paths import data_path


class PaperBroker(Broker):
    name = "paper"
    is_paper = True

    def __init__(
        self,
        starting_cash: float = 10_000.0,
        state_path: Optional[str] = None,
        price_overrides: Optional[Dict[str, float]] = None,
        model_spread: bool = False,
        leverage: float = 1.0,
    ):
        self.state_path = state_path or data_path("paper_account.json")
        self.price_overrides = price_overrides or {}
        self._cash = starting_cash
        self._positions: Dict[str, float] = {}
        # Margin, like the funded accounts this paper account rehearses for
        # (prop firms fund at 1:30-1:100): with leverage > 1 a buy may push
        # cash negative, as long as total notional stays within
        # leverage * equity. 1.0 keeps the old cash-account behavior.
        self.leverage = max(leverage, 1.0)
        # Off by default so every existing offline test's exact expected
        # fill prices stay unchanged; the live desk turns this on (session
        # 27) so its numbers reflect real transaction cost instead of
        # frictionless fills.
        self.model_spread = model_spread
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
        from bots import marketdata

        return marketdata.get_price(symbol)

    def live_spread_pct(self, symbol: str):
        if not self.model_spread:
            return None
        from bots.spreads import spread_pct

        return spread_pct(symbol)

    def _spread_adjusted(self, symbol: str, mid: float, side: str) -> float:
        if not self.model_spread:
            return mid
        from bots.spreads import spread_pct

        half = spread_pct(symbol) / 2.0
        return mid * (1 + half) if side == "buy" else mid * (1 - half)

    def buy(self, symbol: str, quantity: float) -> OrderResult:
        symbol = symbol.upper()
        try:
            fill = self._spread_adjusted(symbol, self.price(symbol), "buy")
        except Exception as exc:
            return OrderResult(False, symbol, "buy", quantity, error=str(exc))
        cost = fill * quantity
        # Exposure after the buy must stay within leverage * equity. In this
        # long-only cash-accounting model exposure == equity - cash, so the
        # bound reduces to: cost <= cash + (leverage - 1) * equity. At
        # leverage 1.0 that is exactly the old cash-account check.
        buying_power = self._cash
        if self.leverage > 1.0:
            buying_power += (self.leverage - 1.0) * max(self.equity(), 0.0)
        if cost > buying_power:
            return OrderResult(
                False, symbol, "buy", quantity,
                error=(
                    f"insufficient buying power: need {cost:.2f}, have "
                    f"{buying_power:.2f} (cash {self._cash:.2f}, "
                    f"leverage {self.leverage:.0f}x)"
                ),
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
            fill = self._spread_adjusted(symbol, self.price(symbol), "sell")
        except Exception as exc:
            return OrderResult(False, symbol, "sell", quantity, error=str(exc))
        self._cash += fill * quantity
        self._positions[symbol] = held - quantity
        if not self._positions[symbol]:
            del self._positions[symbol]
        self._save()
        return OrderResult(True, symbol, "sell", quantity, fill_price=fill, order_id="paper")
