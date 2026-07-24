"""Session 48: desk cycles for GitHub Actions.

GitHub's scheduler has a hard floor of 5 minutes between job STARTS --
confirmed the hard way this session (a 1-minute cron simply never fired,
zero runs in 80+ minutes). That floor is on how often a job starts, not
on what the job does once running. `main()` below exploits that: each
5-minute-triggered job loops internally, checking the market every 60
seconds for its own runtime, so the real-world check cadence is genuinely
1 minute (matching MambaFX's documented style) while the trigger itself
respects GitHub's actual rules.

Each iteration checks the market clock fresh (not just once at job start)
-- NOT a thin wrapper around bots.autopilot.run_autopilot()'s loop, on
purpose: that loop's `cycles` counter only increments when the market is
open, so a run triggered while forex is closed would sleep forever
waiting for max_cycles=1, wrong for a job with a runner timeout. Here,
a closed-market iteration just logs and moves to the next one -- the
loop itself, not run_autopilot(), owns the timing.

Mirrors bots/cli.py's cmd_autopilot() desk construction exactly (funded
config, leverage-aware PaperBroker, realistic spread) so this behaves
identically to `python -m bots autopilot --funded`, just triggered by a
schedule instead of a persistent sleep loop.

    BOT_DATA_DIR=paper_state PYTHONPATH=. python scripts/run_one_cycle.py
"""
from __future__ import annotations

import time
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

CHECK_INTERVAL_SECONDS = 60
# Stop looping with enough margin before the runner's own timeout (and
# before the NEXT scheduled trigger 5 min later) that a slow cycle can
# never overlap the next job -- GitHub Actions concurrency already queues
# overlapping runs, but finishing early avoids relying on that.
LOOP_BUDGET_SECONDS = 4 * 60 + 15  # ~4m15s of a 5-minute window


def run_one_cycle(desk) -> None:
    now = datetime.now(tz=NY)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S ET")
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


def main() -> None:
    config = funded_account_config(timeframe="1m")
    broker = get_broker("paper", model_spread=True, leverage=config.max_leverage)
    desk = TradingDesk(broker=broker, config=config)

    start = time.monotonic()
    checks = 0
    while time.monotonic() - start < LOOP_BUDGET_SECONDS:
        loop_t0 = time.monotonic()
        try:
            run_one_cycle(desk)
        except Exception as exc:
            # Session 51: run_one_cycle_aquafunded.py already had this
            # guard (a single bad cycle -- transient network blip, data
            # provider hiccup -- must not kill the whole job); this
            # script didn't, an inconsistency found while auditing for
            # "the bot never just stops working." Without it, one
            # uncaught exception here fails the whole job, which means
            # the self-chain in trading-cycle.yml's `if: success()` step
            # never fires and this account falls back to the slower
            # 5-minute cron until a cycle happens to succeed clean.
            print(f"cycle failed (will retry next check): {exc}")
        checks += 1
        elapsed_this_check = time.monotonic() - loop_t0
        sleep_for = max(0.0, CHECK_INTERVAL_SECONDS - elapsed_this_check)
        if time.monotonic() - start + sleep_for >= LOOP_BUDGET_SECONDS:
            break
        time.sleep(sleep_for)
    print(f"\n{checks} checks this job run "
          f"({time.monotonic() - start:.0f}s), handing off to the next scheduled trigger")


if __name__ == "__main__":
    main()
