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


def run_autopilot(
    broker_name: str = "paper",
    interval_minutes: int = 30,
    symbols: Optional[list] = None,
    use_llm_committee: bool = False,
    max_cycles: Optional[int] = None,
    desk=None,
    flatten_before_close_minutes: int = 15,
    market_override: Optional[str] = None,
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
        if market_is_open(market):
            mins_left = minutes_to_stock_close(now) if market == "stocks" else None
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
                else:
                    report = desk.run_once(symbols=symbols)
                    print(f"\n[{stamp}] cycle {cycles + 1}:")
                    print(report.describe())
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
