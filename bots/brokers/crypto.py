"""Crypto exchange connector via ccxt (github.com/ccxt/ccxt).

ccxt speaks the API of 100+ exchanges (Binance, Bybit, Coinbase, Kraken, ...)
behind one interface. Many of them offer a sandbox/testnet -- this connector
turns sandbox mode ON by default; pass live=True (and accept real-money risk)
to trade for real.

Install:  pip install ccxt
Keys:     CRYPTO_EXCHANGE (e.g. "binance"), CRYPTO_API_KEY, CRYPTO_API_SECRET
"""

from __future__ import annotations

import os
from typing import Dict

from bots.brokers.base import Broker, OrderResult


class CryptoBroker(Broker):
    name = "crypto"

    def __init__(
        self,
        exchange_id: str = "",
        api_key: str = "",
        secret: str = "",
        live: bool = False,
        quote_currency: str = "USDT",
    ):
        try:
            import ccxt
        except ImportError as exc:
            raise ImportError("ccxt is not installed. Run: pip install ccxt") from exc
        exchange_id = exchange_id or os.environ.get("CRYPTO_EXCHANGE", "binance")
        api_key = api_key or os.environ.get("CRYPTO_API_KEY", "")
        secret = secret or os.environ.get("CRYPTO_API_SECRET", "")
        exchange_cls = getattr(ccxt, exchange_id)
        self.exchange = exchange_cls({"apiKey": api_key, "secret": secret})
        self.is_paper = not live
        if not live:
            try:
                self.exchange.set_sandbox_mode(True)
            except Exception as exc:
                raise RuntimeError(
                    f"{exchange_id} has no ccxt sandbox mode; refusing to default to "
                    "live trading. Pass live=True explicitly to trade real funds."
                ) from exc
        self.quote_currency = quote_currency

    def _pair(self, symbol: str) -> str:
        return symbol if "/" in symbol else f"{symbol.upper()}/{self.quote_currency}"

    def cash(self) -> float:
        balance = self.exchange.fetch_balance()
        return float(balance.get(self.quote_currency, {}).get("free", 0.0) or 0.0)

    def positions(self) -> Dict[str, float]:
        balance = self.exchange.fetch_balance()
        totals = balance.get("total", {}) or {}
        return {
            asset: float(amount)
            for asset, amount in totals.items()
            if amount and asset != self.quote_currency
        }

    def price(self, symbol: str) -> float:
        ticker = self.exchange.fetch_ticker(self._pair(symbol))
        return float(ticker["last"])

    def buy(self, symbol: str, quantity: float) -> OrderResult:
        try:
            order = self.exchange.create_market_buy_order(self._pair(symbol), quantity)
            return OrderResult(True, symbol.upper(), "buy", quantity, order_id=order.get("id"))
        except Exception as exc:
            return OrderResult(False, symbol.upper(), "buy", quantity, error=str(exc))

    def sell(self, symbol: str, quantity: float) -> OrderResult:
        try:
            order = self.exchange.create_market_sell_order(self._pair(symbol), quantity)
            return OrderResult(True, symbol.upper(), "sell", quantity, order_id=order.get("id"))
        except Exception as exc:
            return OrderResult(False, symbol.upper(), "sell", quantity, error=str(exc))
