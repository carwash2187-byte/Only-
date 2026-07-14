"""MetaTrader 5 connector -- the classic forex/CFD platform ("Platform 5").

Uses MetaQuotes' official Python package, which talks to a RUNNING MetaTrader
5 terminal on the SAME machine. That means:

- Windows only (the MetaTrader5 pip package does not support Linux/macOS
  natively; Wine setups exist but are unsupported).
- Install the MT5 terminal from your broker, log into your DEMO account in
  the terminal, enable Tools -> Options -> Expert Advisors -> "Allow
  algorithmic trading", keep the terminal running.
- pip install MetaTrader5

The connector trades whatever account the terminal is logged into. It detects
demo vs real accounts and refuses real accounts unless MT5_LIVE=1 is set --
log the terminal into a demo account first.

Quantities: the desk sizes trades in units of base currency; MT5 orders are
in lots. The conversion uses the symbol's real contract size from the
terminal, rounded to the symbol's volume step.
"""

from __future__ import annotations

import os
from typing import Dict

from bots.brokers.base import Broker, OrderResult


class MetaTrader5Broker(Broker):
    name = "mt5"

    def __init__(self, live: bool = False):
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise ImportError(
                "MetaTrader5 is not installed (Windows only). Run: pip install MetaTrader5"
            ) from exc
        self.mt5 = mt5
        if not mt5.initialize():
            raise RuntimeError(
                f"Could not connect to the MetaTrader 5 terminal: {mt5.last_error()}. "
                "Is the MT5 terminal installed, running, and logged in?"
            )
        info = mt5.account_info()
        if info is None:
            raise RuntimeError("MT5 terminal is running but no account is logged in.")
        # trade_mode 0 = demo account
        self.is_paper = info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
        self.live = live or os.environ.get("MT5_LIVE") == "1"
        if not self.is_paper and not self.live:
            raise RuntimeError(
                "The MT5 terminal is logged into a REAL account. Log into a demo "
                "account, or set MT5_LIVE=1 if you really mean to trade real money."
            )

    def cash(self) -> float:
        return float(self.mt5.account_info().margin_free)

    def equity(self) -> float:
        return float(self.mt5.account_info().equity)

    def positions(self) -> Dict[str, float]:
        result: Dict[str, float] = {}
        for pos in self.mt5.positions_get() or []:
            volume = pos.volume if pos.type == self.mt5.POSITION_TYPE_BUY else -pos.volume
            result[pos.symbol] = result.get(pos.symbol, 0.0) + volume
        return result

    def price(self, symbol: str) -> float:
        symbol = self._resolve(symbol)
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None:
            raise ValueError(f"No MT5 quote for {symbol}")
        return (tick.bid + tick.ask) / 2.0

    def _resolve(self, symbol: str) -> str:
        symbol = symbol.upper().replace("/", "").replace("_", "")
        if self.mt5.symbol_info(symbol) is None:
            raise ValueError(
                f"Symbol {symbol} not found in the MT5 terminal's Market Watch. "
                "Right-click Market Watch -> Show All, or check your broker's naming."
            )
        self.mt5.symbol_select(symbol, True)
        return symbol

    def _units_to_lots(self, symbol: str, quantity: float) -> float:
        info = self.mt5.symbol_info(symbol)
        contract = float(getattr(info, "trade_contract_size", 100_000) or 100_000)
        step = float(getattr(info, "volume_step", 0.01) or 0.01)
        minimum = float(getattr(info, "volume_min", 0.01) or 0.01)
        lots = max(round((quantity / contract) / step) * step, minimum)
        return round(lots, 8)

    def _order(self, symbol: str, quantity: float, side: str) -> OrderResult:
        mt5 = self.mt5
        try:
            symbol = self._resolve(symbol)
            lots = self._units_to_lots(symbol, quantity)
            tick = mt5.symbol_info_tick(symbol)
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": lots,
                "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                "price": tick.ask if side == "buy" else tick.bid,
                "deviation": 20,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
                "comment": "bots-desk",
            }
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                error = getattr(result, "comment", str(mt5.last_error()))
                return OrderResult(False, symbol, side, lots, error=error)
            return OrderResult(True, symbol, side, lots,
                               fill_price=float(result.price) or None,
                               order_id=str(result.order))
        except Exception as exc:
            return OrderResult(False, symbol, side, quantity, error=str(exc))

    def buy(self, symbol: str, quantity: float) -> OrderResult:
        return self._order(symbol, quantity, "buy")

    def sell(self, symbol: str, quantity: float) -> OrderResult:
        return self._order(symbol, quantity, "sell")
