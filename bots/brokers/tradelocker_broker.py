"""TradeLocker connector -- the platform used by many prop firms.

TradeLocker has an official Python API (pip install tradelocker). This
connector talks to the DEMO environment by default; set TRADELOCKER_LIVE=1
only when you truly want real orders.

Environment variables:
    TRADELOCKER_EMAIL      login email
    TRADELOCKER_PASSWORD   login password
    TRADELOCKER_SERVER     server name shown in your TradeLocker credentials
    TRADELOCKER_LIVE=1     switch from demo to live (real money)

Prop-firm note: if this account is a funded/challenge account, read your
firm's automation rules first -- most allow bots ("EAs"), but some restrict
copy trading between accounts, and breaking the rules forfeits the account.

Quantities: the desk sizes trades in units of the base currency; TradeLocker
orders are in lots. For standard forex pairs 1 lot = 100,000 units and the
conversion below applies; for other instruments (indices, metals, crypto CFDs)
contract sizes vary by broker, so the desk quantity is passed through as lots
directly -- start on demo and sanity-check position sizes for those.
"""

from __future__ import annotations

import os
from typing import Dict

from bots.brokers.base import Broker, OrderResult

DEMO_URL = "https://demo.tradelocker.com"
LIVE_URL = "https://live.tradelocker.com"
FOREX_LOT_UNITS = 100_000
MIN_LOT = 0.01


def _is_forex_pair(symbol: str) -> bool:
    s = symbol.upper().replace("/", "").replace("_", "")
    return len(s) == 6 and s.isalpha()


def _units_to_lots(symbol: str, quantity: float) -> float:
    if _is_forex_pair(symbol):
        # floor to 2dp: rounding down keeps the position (and risk) smaller
        lots = int(quantity / FOREX_LOT_UNITS * 100) / 100
        return max(lots, MIN_LOT)
    return quantity


def _clean_symbol(symbol: str) -> str:
    return symbol.upper().replace("_", "").replace("-", "")


class TradeLockerBroker(Broker):
    name = "tradelocker"

    def __init__(self, email: str = "", password: str = "", server: str = "",
                 live: bool = False):
        try:
            from tradelocker import TLAPI
        except ImportError as exc:
            raise ImportError(
                "tradelocker is not installed. Run: pip install tradelocker"
            ) from exc
        email = email or os.environ.get("TRADELOCKER_EMAIL", "")
        password = password or os.environ.get("TRADELOCKER_PASSWORD", "")
        server = server or os.environ.get("TRADELOCKER_SERVER", "")
        if not email or not password or not server:
            raise ValueError(
                "Set TRADELOCKER_EMAIL, TRADELOCKER_PASSWORD and TRADELOCKER_SERVER "
                "(from your broker/prop firm's TradeLocker credentials)."
            )
        self.live = live or os.environ.get("TRADELOCKER_LIVE") == "1"
        self.is_paper = not self.live
        self.api = TLAPI(
            environment=LIVE_URL if self.live else DEMO_URL,
            username=email,
            password=password,
            server=server,
            log_level="warning",
        )

    def _instrument_id(self, symbol: str) -> int:
        return int(self.api.get_instrument_id_from_symbol_name(_clean_symbol(symbol)))

    def cash(self) -> float:
        state = self.api.get_account_state()
        for key in ("availableFunds", "freeMargin", "balance", "accountBalance"):
            if key in state and state[key] is not None:
                return float(state[key])
        return 0.0

    def equity(self) -> float:
        state = self.api.get_account_state()
        for key in ("equity", "projectedBalance", "balance"):
            if key in state and state[key] is not None:
                return float(state[key])
        return self.cash()

    def positions(self) -> Dict[str, float]:
        df = self.api.get_all_positions()
        result: Dict[str, float] = {}
        if df is None or len(df) == 0:
            return result
        for _, row in df.iterrows():
            symbol = self.api.get_symbol_name_from_instrument_id(
                int(row["tradableInstrumentId"])
            )
            qty = float(row["qty"])
            if str(row.get("side", "buy")).lower() == "sell":
                qty = -qty
            result[symbol] = result.get(symbol, 0.0) + qty
        return result

    def price(self, symbol: str) -> float:
        return float(self.api.get_latest_asking_price(self._instrument_id(symbol)))

    def _order(self, symbol: str, quantity: float, side: str) -> OrderResult:
        try:
            lots = _units_to_lots(symbol, quantity)
            order_id = self.api.create_order(
                self._instrument_id(symbol), quantity=lots, side=side, type_="market"
            )
            if not order_id:
                return OrderResult(False, _clean_symbol(symbol), side, lots,
                                   error="order rejected (no order id returned)")
            return OrderResult(True, _clean_symbol(symbol), side, lots,
                               order_id=str(order_id))
        except Exception as exc:
            return OrderResult(False, _clean_symbol(symbol), side, quantity, error=str(exc))

    def buy(self, symbol: str, quantity: float) -> OrderResult:
        return self._order(symbol, quantity, "buy")

    def sell(self, symbol: str, quantity: float) -> OrderResult:
        return self._order(symbol, quantity, "sell")
