"""Autopilot: run trading-desk cycles on a loop, hands-free.

    python -m bots autopilot                          # paper broker, every 30 min
    python -m bots autopilot --broker alpaca          # Alpaca paper account
    python -m bots autopilot --broker oanda --interval 15

Behavior:
- Stock brokers only trade while US markets are open (9:30-16:00 ET, Mon-Fri);
  forex trades around the clock except the weekend gap; crypto never sleeps.
- Every cycle runs the full desk: manage exits, check signals, place entries,
  journal everything. The 5% daily circuit breaker and per-trade risk caps
  apply exactly as in manual mode.
- Ctrl+C stops it cleanly. State (journal, day baseline) survives restarts.

Autopilot is not a profit guarantee -- it's the same desk, on a timer. Check
`python -m bots journal` regularly and stop the loop if the lessons look bad.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

# market kind per broker: which clock its assets trade on
BROKER_MARKET = {
    "paper": "stocks",
    "alpaca": "stocks",
    "robinhood": "stocks",
    "oanda": "forex",
    "tradelocker": "forex",
    "mt5": "forex",
    "crypto": "crypto",
}


def write_last_cycle(desk, report, stamp: str) -> None:
    """Publish this cycle's decisions to BOT_DATA_DIR/last_cycle.json -- the
    command-center dashboard's per-symbol decision feed (session 47). Real
    desk output with the actual reason strings, not a mockup. Never allowed
    to break the trading loop."""
    import json
    from datetime import timezone

    from bots.paths import data_path

    try:
        payload = {
            "stamp": stamp,
            "written_utc": datetime.now(timezone.utc).isoformat(),
            "equity": round(desk.broker.equity(), 2),
            "actions": [
                {
                    "symbol": a.symbol,
                    "action": a.action,
                    "reason": a.reason,
                    "quantity": a.quantity,
                    "ok": a.ok,
                }
                for a in report.actions
            ],
            "notes": list(report.notes),
        }
        with open(data_path("last_cycle.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except Exception:
        pass


def market_is_open(market: str, now: Optional[datetime] = None) -> bool:
    now = (now or datetime.now(tz=NY)).astimezone(NY)
    if market == "crypto":
        return True
    if market == "forex":
        # closed from Friday 17:00 ET to Sunday 17:00 ET
        wd, hour = now.weekday(), now.hour
        if wd == 5:  # Saturday
            return False
        if wd == 4 and hour >= 17:  # Friday evening
            return False
        if wd == 6 and hour < 17:  # Sunday before reopen
            return False
        return True
    # stocks: regular session, ignoring exchange holidays (a cycle on a
    # holiday simply finds no fills/liquidity and does nothing harmful)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def minutes_to_stock_close(now: Optional[datetime] = None) -> Optional[int]:
    """Minutes until the 4pm ET stock close, or None outside the session."""
    now = (now or datetime.now(tz=NY)).astimezone(NY)
    if now.weekday() >= 5:
        return None
    minutes = now.hour * 60 + now.minute
    if not (9 * 60 + 30 <= minutes < 16 * 60):
        return None
    return 16 * 60 - minutes


def select_active_market(
    market: str,
    symbols: Optional[list],
    weekend_symbols: Optional[list],
    stock_symbols: Optional[list],
    weekend_trading_allowed: bool,
    now: Optional[datetime] = None,
) -> tuple:
    """Pick which market/watchlist this cycle actually trades: the weekend
    crypto fallback, the stock session added on top of forex, or just the
    base watchlist. Pulled out of run_autopilot() as a pure function so it's
    directly testable without running the sleep/max_cycles loop -- a mocked
    "market never opens" scenario in that loop spins forever, since cycles
    only increments when a market is actually open.

    Returns (active_market, active_symbols, stock_active).
    """
    active_market, active_symbols = market, symbols
    stock_active = False
    if (
        market == "forex" and weekend_symbols and weekend_trading_allowed
        and not market_is_open("forex", now) and market_is_open("crypto", now)
    ):
        return "crypto", weekend_symbols, False
    if stock_symbols and market_is_open("stocks", now):
        # Add the 9:30-16:00 ET stock session on top of the always-on
        # forex/index watchlist -- same process, same desk, same risk
        # rules, one single writer to the shared journal/account files
        # (running a second autopilot process against the same state
        # directory would race on those JSON writes).
        stock_active = True
        active_symbols = list(dict.fromkeys((symbols or []) + stock_symbols))
    return active_market, active_symbols, stock_active


def run_autopilot(
    broker_name: str = "paper",
    interval_minutes: int = 30,
    symbols: Optional[list] = None,
    use_llm_committee: bool = False,
    max_cycles: Optional[int] = None,
    desk=None,
    flatten_before_close_minutes: int = 15,
    market_override: Optional[str] = None,
    weekend_symbols: Optional[list] = None,
    stock_symbols: Optional[list] = None,
) -> None:
    # The generic paper/Alpaca/Robinhood brokers can hold any symbol
    # (stocks, or crypto tickers like BTC-USD) -- the broker name alone
    # doesn't tell you which market clock applies. When every symbol in
    # the watchlist is crypto, trade around the clock instead of defaulting
    # to stock hours and reporting "closed" 24/7 for a crypto session.
    market = market_override or BROKER_MARKET.get(broker_name, "stocks")
    if market_override is None and symbols and broker_name in ("paper", "alpaca", "robinhood"):
        if all(s.upper().endswith(("-USD", "-USDT")) for s in symbols):
            market = "crypto"
    if desk is None:
        from bots.brokers import get_broker
        from bots.organization import DeskConfig, TradingDesk

        desk = TradingDesk(
            broker=get_broker(broker_name),
            config=DeskConfig(use_llm_committee=use_llm_committee),
        )
    broker = desk.broker
    day_trading = getattr(desk.config, "day_trading", False)
    print(
        f"Autopilot started: broker={broker_name} (paper={broker.is_paper}), "
        f"cycle every {interval_minutes} min, market clock: {market}, "
        f"timeframe={getattr(desk.config, 'timeframe', '1d')}, "
        f"day_trading={day_trading}. Ctrl+C to stop."
    )

    cycles = 0
    flattened_today = None  # date already flattened, so it only fires once
    while max_cycles is None or cycles < max_cycles:
        now = datetime.now(tz=NY)
        stamp = now.strftime("%Y-%m-%d %H:%M ET")
        # Weekend fallback: forex is flat-out closed roughly Fri 5pm ET to
        # Sun 5pm ET. Rather than sitting idle for two days (no shot at the
        # daily target at all on those days), fall back to crypto -- the
        # one market that's actually open then -- when the caller has
        # opted in with a crypto watchlist for it AND this account's rules
        # actually allow weekend trading (some funded firms ban it outright).
        weekend_ok = getattr(desk.config, "weekend_trading_allowed", True)
        active_market, active_symbols, stock_active = select_active_market(
            market, symbols, weekend_symbols, stock_symbols, weekend_ok, now
        )
        if market_is_open(active_market, now):
            mins_left = minutes_to_stock_close(now) if active_market == "stocks" else None
            stock_mins_left = minutes_to_stock_close(now) if stock_active else None
            try:
                if (
                    day_trading
                    and mins_left is not None
                    and mins_left <= flatten_before_close_minutes
                    and flattened_today != now.date()
                ):
                    report = desk.flatten_all()
                    flattened_today = now.date()
                    print(f"\n[{stamp}] day-trading flatten ({mins_left} min to close):")
                    print(report.describe())
                elif (
                    day_trading
                    and stock_mins_left is not None
                    and stock_mins_left <= flatten_before_close_minutes
                    and flattened_today != now.date()
                ):
                    # Only close the stock leg -- forex/index positions are
                    # still tradeable after the NYSE closes and shouldn't
                    # get swept up in the stock session's close-out.
                    report = desk.flatten_all(
                        reason="day-trading: flatten stock positions before the 4pm close",
                        symbols=set(stock_symbols),
                    )
                    flattened_today = now.date()
                    print(f"\n[{stamp}] day-trading flatten stock positions ({stock_mins_left} min to close):")
                    print(report.describe())
                else:
                    label = f" (weekend fallback: {active_market})" if active_market != market else ""
                    report = desk.run_once(symbols=active_symbols)
                    print(f"\n[{stamp}] cycle {cycles + 1}{label}:")
                    print(report.describe())
                write_last_cycle(desk, report, stamp)
            except Exception as exc:
                print(f"\n[{stamp}] cycle failed (will retry next interval): {exc}")
            cycles += 1
        else:
            flattened_today = None  # reset the guard once the session ends
            print(f"[{stamp}] market closed, sleeping...")
        try:
            time.sleep(interval_minutes * 60)
        except KeyboardInterrupt:
            print("\nAutopilot stopped.")
            return
