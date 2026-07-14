"""Alpaca connector -- the recommended way to trade US stocks from code.

Alpaca (https://alpaca.markets) has an official, free API with a built-in
paper-trading mode: sign up, grab API keys, and this connector trades a fake
$100k account until you explicitly switch to a funded live account.

Environment variables:
    ALPACA_API_KEY_ID
    ALPACA_API_SECRET_KEY
    ALPACA_LIVE=1        # only set this when you truly want real-money orders

Uses plain REST via requests, so no extra SDK install is needed.
"""

from __future__ import annotations

import os
from typing import Dict

import requests

from bots.brokers.base import Broker, OrderResult

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
TIMEOUT = 30


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, key_id: str = "", secret_key: str = "", live: bool = False):
        self.key_id = key_id or os.environ.get("ALPACA_API_KEY_ID", "")
        self.secret_key = secret_key or os.environ.get("ALPACA_API_SECRET_KEY", "")
        if not self.key_id or not self.secret_key:
            raise ValueError(
                "Alpaca keys missing. Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY "
                "(free paper-trading keys at https://alpaca.markets)."
            )
        self.live = live or os.environ.get("ALPACA_LIVE") == "1"
        self.is_paper = not self.live
        self.base_url = LIVE_URL if self.live else PAPER_URL

    def _headers(self) -> Dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
        }

    def _get(self, base: str, path: str) -> dict:
        resp = requests.get(base + path, headers=self._headers(), timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def cash(self) -> float:
        return float(self._get(self.base_url, "/v2/account")["cash"])

    def positions(self) -> Dict[str, float]:
        return {
            p["symbol"]: float(p["qty"]) for p in self._get(self.base_url, "/v2/positions")
        }

    def price(self, symbol: str) -> float:
        data = self._get(DATA_URL, f"/v2/stocks/{symbol.upper()}/trades/latest")
        return float(data["trade"]["p"])

    def has_pending_order(self, symbol: str) -> bool:
        # Market orders placed outside trading hours sit as "open" until the
        # next session instead of filling immediately, so this catches the
        # case positions() can't see yet.
        orders = self._get(
            self.base_url,
            f"/v2/orders?status=open&symbols={symbol.upper()}",
        )
        return len(orders) > 0

    def _order(self, symbol: str, quantity: float, side: str) -> OrderResult:
        payload = {
            "symbol": symbol.upper(),
            "qty": str(quantity),
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }
        try:
            resp = requests.post(
                self.base_url + "/v2/orders",
                headers=self._headers(),
                json=payload,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            order = resp.json()
            return OrderResult(
                True, symbol.upper(), side, quantity, order_id=order.get("id")
            )
        except requests.RequestException as exc:
            detail = ""
            if getattr(exc, "response", None) is not None:
                detail = f" ({exc.response.text[:200]})"
            return OrderResult(False, symbol.upper(), side, quantity, error=str(exc) + detail)

    def buy(self, symbol: str, quantity: float) -> OrderResult:
        return self._order(symbol, quantity, "buy")

    def sell(self, symbol: str, quantity: float) -> OrderResult:
        return self._order(symbol, quantity, "sell")
