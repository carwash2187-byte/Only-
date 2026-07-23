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

Funded-account safety properties of this connector:
- Every desk entry goes through buy_bracket(), which attaches the stop-loss
  and take-profit to the order itself (TradeLocker enforces them server-side).
  A container restart between desk cycles leaves the position protected.
  If the bracket is rejected, NO position is opened -- a missed trade is
  recoverable, an unprotected position on a funded account is not.
- sell() closes existing long positions through the position endpoint
  instead of submitting a naked sell order. On hedging-mode accounts (the
  prop-firm default) a naked sell would OPEN a short next to the long,
  doubling margin, rather than exiting.
- Watchlist names (GOLD, OIL, US30, NAS100, US500) are translated to
  whatever this particular firm calls the instrument (XAUUSD, USOIL,
  US100...), and positions() translates back, so the desk's journal
  reconciliation never mistakes a renamed symbol for a closed trade.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from bots.brokers.base import Broker, OrderResult

DEMO_URL = "https://demo.tradelocker.com"
LIVE_URL = "https://live.tradelocker.com"
FOREX_LOT_UNITS = 100_000
MIN_LOT = 0.01

# Desk watchlist name -> candidate TradeLocker instrument names, tried in
# order. Prop firms name CFDs differently (XAUUSD vs GOLD, US100 vs NAS100);
# the first name that resolves on the connected account wins and is cached.
TRADELOCKER_ALIASES: Dict[str, list] = {
    "GOLD": ["XAUUSD", "GOLD"],
    "SILVER": ["XAGUSD", "SILVER"],
    # WTI added session 48: confirmed via AquaFunded's real account
    # instrument list (api.get_all_instruments()) -- their EQUITY_CFD name
    # for WTI crude is literally "WTI", none of the previously-tried
    # aliases matched on this broker.
    "OIL": ["USOIL", "XTIUSD", "WTIUSD", "CRUDEOIL", "WTI", "OIL"],
    "NAS100": ["NAS100", "US100", "USTEC", "NDX100"],
    "US30": ["US30", "DJ30", "DOW30"],
    "US500": ["US500", "SPX500", "SP500"],
    "US2000": ["US2000", "RUS2000", "RTY2000", "USSC2000"],
    # weekend fallback symbols keep their Yahoo-style desk names in the
    # journal, so positions must map back to them, dash included
    "BTC-USD": ["BTCUSD"],
    "ETH-USD": ["ETHUSD"],
    "SOL-USD": ["SOLUSD"],
}

# TradeLocker name -> canonical watchlist name, for mapping open positions
# back after a restart (before any forward lookup has warmed the cache).
_REVERSE_ALIASES: Dict[str, str] = {
    candidate: desk
    for desk, candidates in TRADELOCKER_ALIASES.items()
    for candidate in candidates
    if candidate != desk
}


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
        self._id_cache: Dict[str, int] = {}
        self._desk_symbol_by_id: Dict[int, str] = {}
        self.api = TLAPI(
            environment=LIVE_URL if self.live else DEMO_URL,
            username=email,
            password=password,
            server=server,
            log_level="warning",
        )

    def _instrument_id(self, symbol: str) -> int:
        # cache under the desk's own name (dashes and all) -- positions()
        # must hand back exactly what the journal recorded
        key = symbol.upper()
        if key in self._id_cache:
            return self._id_cache[key]
        candidates = list(
            dict.fromkeys(TRADELOCKER_ALIASES.get(key, []) + [_clean_symbol(symbol)])
        )
        for name in candidates:
            try:
                iid = int(self.api.get_instrument_id_from_symbol_name(name))
            except Exception:
                continue
            self._id_cache[key] = iid
            self._desk_symbol_by_id[iid] = key
            return iid
        raise ValueError(
            f"TradeLocker: no instrument found for {symbol} "
            f"(tried {', '.join(candidates)}). Run scripts/preflight_funded.py "
            "to see what this account actually calls it."
        )

    def _desk_symbol(self, instrument_id: int) -> str:
        """Map a position's instrument back to the name the desk/journal uses."""
        if instrument_id in self._desk_symbol_by_id:
            return self._desk_symbol_by_id[instrument_id]
        tl_name = str(
            self.api.get_symbol_name_from_instrument_id(instrument_id)
        ).upper()
        desk_name = _REVERSE_ALIASES.get(tl_name, tl_name)
        self._desk_symbol_by_id[instrument_id] = desk_name
        return desk_name

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
            symbol = self._desk_symbol(int(row["tradableInstrumentId"]))
            qty = float(row["qty"])
            if str(row.get("side", "buy")).lower() == "sell":
                qty = -qty
            result[symbol] = result.get(symbol, 0.0) + qty
        return result

    def position_entry_price(self, symbol: str) -> Optional[float]:
        """Broker-reported average entry price for an open position, or None.

        Session 48 (user's own words): "even if I click on a trade myself,
        make it protect it." The desk's own stop-loss/breakeven/trailing
        logic needs an entry price to work from -- for a position the desk
        opened itself that comes from the journal, but for a position
        opened by hand directly in the TradeLocker app the desk has no
        record of it. This reads TradeLocker's own reported average price
        for that position so the desk can adopt it into the journal (see
        TradingDesk._adopt_untracked_positions) and give it the SAME real
        protection as any bot-opened trade, not just a best-effort fallback.
        """
        try:
            iid = self._instrument_id(symbol)
        except Exception:
            return None
        df = self.api.get_all_positions()
        if df is None or len(df) == 0:
            return None
        matches = df[df["tradableInstrumentId"] == iid]
        if len(matches) == 0:
            return None
        for col in ("avgPrice", "openPrice", "avgPricePerUnit"):
            if col in matches.columns:
                val = matches.iloc[0][col]
                if val is not None:
                    return float(val)
        return None

    def price(self, symbol: str) -> float:
        return float(self.api.get_latest_asking_price(self._instrument_id(symbol)))

    def live_spread_pct(self, symbol: str):
        try:
            iid = self._instrument_id(symbol)
            ask = float(self.api.get_latest_asking_price(iid))
            bid = float(self.api.get_latest_bid_price(iid))
            mid = (ask + bid) / 2.0
            if mid <= 0 or ask < bid:
                return None
            return (ask - bid) / mid
        except Exception:
            return None

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

    def buy_bracket(
        self, symbol: str, quantity: float, stop_loss_pct: float, take_profit_pct: float
    ) -> OrderResult:
        """Entry with server-side stop-loss and take-profit attached.

        Uses TradeLocker's offset type so both legs are measured from the
        actual fill price, not the pre-order quote. There is deliberately
        no fallback to an unprotected plain buy: on a funded account the
        desk's polled stops vanish whenever this container restarts, and
        a position with no server-side stop across that gap is exactly
        how a 3% daily limit gets blown. No bracket, no trade.
        """
        try:
            iid = self._instrument_id(symbol)
            lots = _units_to_lots(symbol, quantity)
            ref_price = float(self.api.get_latest_asking_price(iid))
            order_id = self.api.create_order(
                iid, quantity=lots, side="buy", type_="market",
                stop_loss=round(ref_price * stop_loss_pct, 5),
                stop_loss_type="offset",
                take_profit=round(ref_price * take_profit_pct, 5),
                take_profit_type="offset",
            )
            if not order_id:
                return OrderResult(
                    False, _clean_symbol(symbol), "buy", lots,
                    error="bracket order rejected -- refusing to enter unprotected",
                )
            return OrderResult(True, _clean_symbol(symbol), "buy", lots,
                               order_id=str(order_id))
        except Exception as exc:
            return OrderResult(False, _clean_symbol(symbol), "buy", quantity,
                               error=f"bracket order failed ({exc}) -- no unprotected entry")

    def sell(self, symbol: str, quantity: float) -> OrderResult:
        """Exit: close ANY open position on this symbol via the position
        endpoint, long or short.

        BUG FOUND AND FIXED (session 48): this used to only close "buy"
        (long) positions, on the stale assumption "the desk is long-only."
        That assumption stopped being true once the desk started adopting
        untracked positions (see TradingDesk._adopt_untracked_positions),
        which can be short. The old code silently skipped short positions
        in the closing loop, `closed` stayed 0, and fell through to a
        useless plain sell order every single cycle -- caught live: a
        short AUDUSD position repeated "SELL AUDUSD ... locking in the
        day | realized PnL +0.00" every cycle for hours, never actually
        closing, because there was nothing here that knew how to close a
        short. Closing a short through the position endpoint (not a naked
        opposite-direction order) is still correct on a hedging-mode
        account -- it's a genuine close, not "open a long alongside it."
        """
        try:
            iid = self._instrument_id(symbol)
            lots_wanted = _units_to_lots(symbol, quantity)
            df = self.api.get_all_positions()
            closed = 0.0
            if df is not None and len(df):
                mine = df[df["tradableInstrumentId"] == iid]
                for _, row in mine.iterrows():
                    remaining = lots_wanted - closed
                    if remaining <= 0:
                        break
                    pos_qty = float(row["qty"])
                    take = min(pos_qty, remaining)
                    # BUG FOUND AND FIXED (session 48, round 2): close_position's
                    # own docstring says close_quantity=0 means "close the whole
                    # position" -- true ONLY on its order_id code path. On the
                    # position_id path (what we use here), the library passes
                    # close_quantity straight through as the literal `qty` field
                    # of the DELETE request with NO substitution -- close_quantity=0
                    # sends qty="0" to TradeLocker, a request to close ZERO units.
                    # The API still returns ok=True (order accepted), so our code
                    # logged a fake "realized PnL +0.00" success every cycle while
                    # the real position never shrank at all -- this is what kept
                    # the live short AUDUSD position open through the whole first
                    # round of fixes. Always pass the real quantity to close.
                    ok = self.api.close_position(
                        position_id=int(row["id"]),
                        close_quantity=take,
                    )
                    if ok:
                        closed += take
            if closed > 0:
                return OrderResult(True, _clean_symbol(symbol), "sell", closed)
            return self._order(symbol, quantity, "sell")
        except Exception as exc:
            return OrderResult(False, _clean_symbol(symbol), "sell", quantity,
                               error=str(exc))
