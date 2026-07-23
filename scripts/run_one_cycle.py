"""Session 48: ONE desk cycle, then exit. Built for GitHub Actions (a cron
job, not a persistent process) -- NOT a thin wrapper around
bots.autopilot.run_autopilot()'s loop, on purpose: that loop's `cycles`
counter only increments when the market is actually open, so a run
triggered while forex is closed would sleep forever waiting for
max_cycles=1 to be satisfied, which is exactly wrong for a cron job with a
runner timeout. This script checks the market clock once, does at most one
cycle of real work, and returns immediately either way.

Mirrors bots/cli.py's cmd_autopilot() desk construction exactly (funded
config, leverage-aware PaperBroker, realistic spread) so a GitHub Actions
run behaves identically to the live `python -m bots autopilot --funded`
command it replaces -- same account, same rules, just triggered by a
schedule instead of a sleep loop.

    BOT_DATA_DIR=paper_state PYTHONPATH=. python scripts/run_one_cycle.py
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bots.autopilot import (
    market_is_open,
    minutes_to_stock_close,
    select_active_market,
    write_last_cycle,
)
from bots.brokers import get_broker
from bots.organization import TradingDesk, funded_account_config

NY = ZoneInfo("America/New_York")

# Same watchlist as CLAUDE.md's documented live launch command.
SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCHF", "USDCAD",
    "EURJPY", "GBPJPY", "AUDJPY", "EURGBP", "EURCHF",
    "US30", "NAS100", "US500", "US2000", "GOLD", "SILVER", "OIL",
]
WEEKEND_SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD"]


def main() -> None:
    config = funded_account_config(timeframe="1m")
    broker = get_broker("paper", model_spread=True, leverage=config.max_leverage)
    desk = TradingDesk(broker=broker, config=config)

    now = datetime.now(tz=NY)
    stamp = now.strftime("%Y-%m-%d %H:%M ET")
    weekend_ok = getattr(desk.config, "weekend_trading_allowed", True)
    active_market, active_symbols, _stock_active = select_active_market(
        "forex", SYMBOLS, WEEKEND_SYMBOLS, None, weekend_ok, now
    )

    if not market_is_open(active_market, now):
        print(f"[{stamp}] market closed ({active_market}) -- nothing to do this cycle")
        return

    day_trading = getattr(desk.config, "day_trading", False)
    mins_left = minutes_to_stock_close(now) if active_market == "stocks" else None
    if day_trading and mins_left is not None and mins_left <= 15:
        report = desk.flatten_all()
        print(f"[{stamp}] day-trading flatten ({mins_left} min to close):")
    else:
        label = f" (weekend fallback: {active_market})" if active_market != "forex" else ""
        report = desk.run_once(symbols=active_symbols)
        print(f"[{stamp}] cycle{label}:")
    print(report.describe())
    write_last_cycle(desk, report, stamp)


if __name__ == "__main__":
    main()
