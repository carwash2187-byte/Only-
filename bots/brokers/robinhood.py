"""Robinhood connector via the robin_stocks library.

IMPORTANT: Robinhood has NO official trading API for individuals. robin_stocks
(https://github.com/jmfernandes/robin_stocks) reverse-engineers the app's
private API. It generally works, but:
- it can break without notice when Robinhood changes things,
- automated trading may violate Robinhood's terms of service and can get an
  account restricted,
- there is no paper-trading mode -- every order here is REAL MONEY.

For automated stock trading, prefer the Alpaca connector (official API with
free paper trading). Use this one only if you accept the risks above.

Install:  pip install robin_stocks
Login:    set ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD env vars (an MFA prompt
          may appear in the terminal on first login; robin_stocks caches the
          session token afterwards).
"""

from __future__ import annotations

import os
from typing import Dict

from bots.brokers.base import Broker, OrderResult


class RobinhoodBroker(Broker):
    name = "robinhood"
    is_paper = False  # no paper mode exists -- all orders are live

    def __init__(self, username: str = "", password: str = ""):
        try:
            import robin_stocks.robinhood as rh
        except ImportError as exc:
            raise ImportError(
                "robin_stocks is not installed. Run: pip install robin_stocks"
            ) from exc
        self.rh = rh
        username = username or os.environ.get("ROBINHOOD_USERNAME", "")
        password = password or os.environ.get("ROBINHOOD_PASSWORD", "")
        if not username or not password:
            raise ValueError(
                "Set ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD environment variables."
            )
        self.rh.login(username, password)

    def cash(self) -> float:
        profile = self.rh.profiles.load_account_profile()
        return float(profile.get("buying_power", 0.0))

    def positions(self) -> Dict[str, float]:
        holdings = self.rh.account.build_holdings()
        return {symbol: float(info["quantity"]) for symbol, info in holdings.items()}

    def price(self, symbol: str) -> float:
        quotes = self.rh.stocks.get_latest_price(symbol.upper())
        return float(quotes[0])

    def buy(self, symbol: str, quantity: float) -> OrderResult:
        try:
            order = self.rh.orders.order_buy_market(symbol.upper(), quantity)
            order_id = (order or {}).get("id")
            if not order_id:
                return OrderResult(False, symbol.upper(), "buy", quantity, error=str(order))
            return OrderResult(True, symbol.upper(), "buy", quantity, order_id=order_id)
        except Exception as exc:
            return OrderResult(False, symbol.upper(), "buy", quantity, error=str(exc))

    def sell(self, symbol: str, quantity: float) -> OrderResult:
        try:
            order = self.rh.orders.order_sell_market(symbol.upper(), quantity)
            order_id = (order or {}).get("id")
            if not order_id:
                return OrderResult(False, symbol.upper(), "sell", quantity, error=str(order))
            return OrderResult(True, symbol.upper(), "sell", quantity, order_id=order_id)
        except Exception as exc:
            return OrderResult(False, symbol.upper(), "sell", quantity, error=str(exc))
