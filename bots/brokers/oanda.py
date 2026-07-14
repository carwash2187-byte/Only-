"""OANDA connector -- official, free forex API with practice accounts.

Forex scalping the safe way: OANDA (https://www.oanda.com) gives out free
practice ("fxTrade Practice") accounts with fake money and the exact same v20
REST API as live accounts. This connector talks to the practice endpoint
unless you explicitly set OANDA_LIVE=1.

Setup:
  1. Create a free OANDA account, open the demo/practice account.
  2. Generate a personal API token (Account -> Manage API Access).
  3. export OANDA_API_TOKEN=...   OANDA_ACCOUNT_ID=101-001-1234567-001

Symbols: pass "EURUSD", "EUR/USD" or "EUR_USD" -- all normalized to OANDA's
EUR_USD form. Quantities are units of the base currency (integer).
"""

from __future__ import annotations

import os
from typing import Dict

import requests

from bots.brokers.base import Broker, OrderResult

PRACTICE_URL = "https://api-fxpractice.oanda.com"
LIVE_URL = "https://api-fxtrade.oanda.com"
TIMEOUT = 30


def _instrument(symbol: str) -> str:
    s = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
    if len(s) == 6 and s.isalpha():
        return f"{s[:3]}_{s[3:]}"
    return symbol.upper().replace("/", "_")


class OandaBroker(Broker):
    name = "oanda"

    def __init__(self, api_token: str = "", account_id: str = "", live: bool = False):
        self.token = api_token or os.environ.get("OANDA_API_TOKEN", "")
        self.account_id = account_id or os.environ.get("OANDA_ACCOUNT_ID", "")
        if not self.token or not self.account_id:
            raise ValueError(
                "OANDA credentials missing. Set OANDA_API_TOKEN and OANDA_ACCOUNT_ID "
                "(free practice account at oanda.com)."
            )
        self.live = live or os.environ.get("OANDA_LIVE") == "1"
        self.is_paper = not self.live
        self.base_url = LIVE_URL if self.live else PRACTICE_URL

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = requests.get(
            self.base_url + path, headers=self._headers(), params=params, timeout=TIMEOUT
        )
        resp.raise_for_status()
        return resp.json()

    def cash(self) -> float:
        summary = self._get(f"/v3/accounts/{self.account_id}/summary")["account"]
        return float(summary["marginAvailable"])

    def equity(self) -> float:
        summary = self._get(f"/v3/accounts/{self.account_id}/summary")["account"]
        return float(summary["NAV"])

    def positions(self) -> Dict[str, float]:
        data = self._get(f"/v3/accounts/{self.account_id}/openPositions")
        result: Dict[str, float] = {}
        for pos in data.get("positions", []):
            units = float(pos["long"]["units"]) + float(pos["short"]["units"])
            if units:
                result[pos["instrument"]] = units
        return result

    def price(self, symbol: str) -> float:
        data = self._get(
            f"/v3/accounts/{self.account_id}/pricing",
            params={"instruments": _instrument(symbol)},
        )
        quote = data["prices"][0]
        bid = float(quote["bids"][0]["price"])
        ask = float(quote["asks"][0]["price"])
        return (bid + ask) / 2.0

    def _order(self, symbol: str, units: int) -> OrderResult:
        side = "buy" if units > 0 else "sell"
        payload = {
            "order": {
                "type": "MARKET",
                "instrument": _instrument(symbol),
                "units": str(units),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
            }
        }
        try:
            resp = requests.post(
                f"{self.base_url}/v3/accounts/{self.account_id}/orders",
                headers=self._headers(),
                json=payload,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            fill = data.get("orderFillTransaction", {})
            if not fill:
                reason = data.get("orderCancelTransaction", {}).get("reason", "not filled")
                return OrderResult(False, _instrument(symbol), side, abs(units), error=reason)
            return OrderResult(
                True,
                _instrument(symbol),
                side,
                abs(units),
                fill_price=float(fill.get("price") or 0) or None,
                order_id=fill.get("id"),
            )
        except requests.RequestException as exc:
            detail = ""
            if getattr(exc, "response", None) is not None:
                detail = f" ({exc.response.text[:200]})"
            return OrderResult(False, _instrument(symbol), side, abs(units), error=str(exc) + detail)

    def buy(self, symbol: str, quantity: float) -> OrderResult:
        units = int(quantity)
        if units <= 0:
            return OrderResult(False, _instrument(symbol), "buy", quantity,
                               error="forex quantity must be >= 1 unit")
        return self._order(symbol, units)

    def sell(self, symbol: str, quantity: float) -> OrderResult:
        units = int(quantity)
        if units <= 0:
            return OrderResult(False, _instrument(symbol), "sell", quantity,
                               error="forex quantity must be >= 1 unit")
        return self._order(symbol, -units)
