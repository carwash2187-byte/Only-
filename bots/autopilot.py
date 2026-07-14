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


def run_autopilot(
    broker_name: str = "paper",
    interval_minutes: int = 30,
    symbols: Optional[list] = None,
    use_llm_committee: bool = False,
    max_cycles: Optional[int] = None,
    desk=None,
) -> None:
    market = BROKER_MARKET.get(broker_name, "stocks")
    if desk is None:
        from bots.brokers import get_broker
        from bots.organization import DeskConfig, TradingDesk

        desk = TradingDesk(
            broker=get_broker(broker_name),
            config=DeskConfig(use_llm_committee=use_llm_committee),
        )
    broker = desk.broker
    print(
        f"Autopilot started: broker={broker_name} (paper={broker.is_paper}), "
        f"cycle every {interval_minutes} min, market clock: {market}. Ctrl+C to stop."
    )

    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        stamp = datetime.now(tz=NY).strftime("%Y-%m-%d %H:%M ET")
        if market_is_open(market):
            try:
                report = desk.run_once(symbols=symbols)
                print(f"\n[{stamp}] cycle {cycles + 1}:")
                print(report.describe())
            except Exception as exc:
                print(f"\n[{stamp}] cycle failed (will retry next interval): {exc}")
            cycles += 1
        else:
            print(f"[{stamp}] market closed, sleeping...")
        try:
            time.sleep(interval_minutes * 60)
        except KeyboardInterrupt:
            print("\nAutopilot stopped.")
            return
