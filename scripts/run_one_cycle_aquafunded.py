"""Session 48: ONE desk cycle for the AquaFunded TradeLocker account, then
exit. Same reasoning as scripts/run_one_cycle.py (built for a cron job, not
a persistent loop -- see that file's docstring for why the naive
run_autopilot() loop is wrong for this). This is the AquaFunded-specific
counterpart: TradeLocker broker, aquafunded_instant_config() (the
CONFIRMED real 3%/6% rules, not generic defaults -- see CLAUDE.md's
"funded-account rule presets are law").

Credentials come ONLY from environment variables (GitHub Actions repository
secrets when run there) -- never hardcoded, never read from a committed
file. TRADELOCKER_LIVE is deliberately NOT set here; add it only after an
explicit, separate decision to go live -- this script defaults to safe.

    TRADELOCKER_EMAIL=... TRADELOCKER_PASSWORD=... TRADELOCKER_SERVER=AQUA \
        BOT_DATA_DIR=funded_state_aquafunded PYTHONPATH=. \
        python scripts/run_one_cycle_aquafunded.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from bots.autopilot import market_is_open, select_active_market, write_last_cycle
from bots.brokers.tradelocker_broker import TradeLockerBroker
from bots.organization import TradingDesk, aquafunded_instant_config

NY = ZoneInfo("America/New_York")

SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCHF", "USDCAD",
    "EURJPY", "GBPJPY", "AUDJPY", "EURGBP", "EURCHF",
    "US30", "NAS100", "US500", "US2000", "GOLD", "SILVER", "OIL",
]


def main() -> None:
    for var in ("TRADELOCKER_EMAIL", "TRADELOCKER_PASSWORD", "TRADELOCKER_SERVER"):
        if not os.environ.get(var):
            print(f"FAIL: {var} is not set -- refusing to start")
            sys.exit(1)

    config = aquafunded_instant_config()
    live = os.environ.get("TRADELOCKER_LIVE") == "1"
    try:
        broker = TradeLockerBroker(live=live)
    except Exception as exc:
        print(f"FAIL: could not connect to TradeLocker: {exc}")
        sys.exit(1)
    desk = TradingDesk(broker=broker, config=config)

    now = datetime.now(tz=NY)
    stamp = now.strftime("%Y-%m-%d %H:%M ET")
    # AquaFunded's weekend-trading policy is unconfirmed (see
    # aquafunded_instant_config()'s docstring) -- weekend_symbols is
    # deliberately None here, not the crypto fallback, until that's
    # verified. This account only trades while forex is actually open.
    active_market, active_symbols, _ = select_active_market(
        "forex", SYMBOLS, None, None, config.weekend_trading_allowed, now
    )
    if not market_is_open(active_market, now):
        print(f"[{stamp}] market closed ({active_market}) -- nothing to do this cycle")
        return

    report = desk.run_once(symbols=active_symbols)
    print(f"[{stamp}] cycle (env={'LIVE' if live else 'demo'}):")
    print(report.describe())
    write_last_cycle(desk, report, stamp)


if __name__ == "__main__":
    main()
