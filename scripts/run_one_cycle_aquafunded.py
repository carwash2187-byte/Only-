"""Session 48: desk cycles for the AquaFunded TradeLocker account, for
GitHub Actions. Same reasoning as scripts/run_one_cycle.py for why a job
loops internally every 60s instead of exiting after one check (GitHub's
scheduler floor is 5 minutes between job STARTS, not between checks a
running job makes -- this gets real 1-minute cadence, matching MambaFX's
documented style, within that real platform limit).

Connects to TradeLocker ONCE per job run, then loops run_once() calls
against that same connection -- reconnecting (logging in again) every
60 seconds would be wasteful and risks the broker's own rate limits.

aquafunded_instant_config() is the CONFIRMED real 3%/6% AquaFunded rules,
not generic defaults (see CLAUDE.md's "funded-account rule presets are
law").

Credentials come ONLY from environment variables (GitHub Actions repository
secrets when run there) -- never hardcoded, never read from a committed
file.

CORRECTED (session 48): this connector is hardcoded to live=False. Verified
against AquaFunded's own FAQ and TradeLocker's account switcher -- every
account this firm issues is a permanent simulated/DEMO account by design
("AquaFunded's Funded Accounts should not be considered live trading
accounts... All accounts provided by AquaFunded are demo accounts with
virtual funds"). There is no live server for this product to ever connect
to; real payouts happen through AquaFunded's own "Request Payout" flow
against this same account, not a different live connection. An earlier
version of this script derived live-mode from an AQUAFUNDED_GO_LIVE /
TRADELOCKER_LIVE two-key gate, modeled on a wrong assumption that a
separate live account existed -- every attempt 401'd against
live.tradelocker.com and (correctly, by the runaway-loop fix) stalled the
self-chain each time. See .github/workflows/trading-cycle-aquafunded.yml's
comments for the full incident writeup.

    TRADELOCKER_EMAIL=... TRADELOCKER_PASSWORD=... TRADELOCKER_SERVER=AQUA \
        BOT_DATA_DIR=funded_state_aquafunded PYTHONPATH=. \
        python scripts/run_one_cycle_aquafunded.py
"""
from __future__ import annotations

import os
import sys
import time
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
    # Session 49 (user directive): pre-London European indices, priority
    # window 2-4am ET via TIMED_SESSION_FOCUS. Uses the broker's own
    # verified-alias fallback (bots/brokers/tradelocker_broker.py) -- if
    # this account's TradeLocker doesn't recognize any of the tried names,
    # a clean error names the missing symbol instead of trading blind.
    "DAX", "UK100",
]

# Session 53: weekend crypto fallback, same symbols as the paper account's
# WEEKEND_SYMBOLS. Confirmed directly from AquaFunded's own help center
# (https://help.aquafunded.com/en/articles/11831680, not a review site --
# see CLAUDE.md's evidence bar): "Yes, you can hold trades overnight and
# over the weekend... There are no restrictions... crypto accounts:
# trading is available 24/7, including weekends." No distinction drawn
# between challenge and funded accounts. This resolves the "unconfirmed"
# caveat that used to keep weekend_symbols=None below. If this account's
# TradeLocker doesn't recognize a symbol name, _instrument_id raises a
# clean error naming it (same safety pattern as DAX/UK100 above) --
# never trades blind on an unresolved symbol.
WEEKEND_SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD"]

CHECK_INTERVAL_SECONDS = 60
LOOP_BUDGET_SECONDS = 4 * 60 + 15  # ~4m15s of a 5-minute window, same margin as the paper script


def run_one_cycle(desk, live: bool) -> None:
    now = datetime.now(tz=NY)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S ET")
    # Session 53: weekend crypto fallback now enabled -- see WEEKEND_SYMBOLS
    # above for the citation (AquaFunded's own help center, no restrictions
    # on weekend/crypto trading). Same risk rules (3%/6%, ATR stops,
    # correlation caps) apply to these trades as to forex -- this only
    # changes which market is checked, not how carefully it's traded.
    active_market, active_symbols, _ = select_active_market(
        "forex", SYMBOLS, WEEKEND_SYMBOLS, None,
        desk.config.weekend_trading_allowed, now
    )
    if not market_is_open(active_market, now):
        print(f"[{stamp}] market closed ({active_market}) -- nothing to do this cycle")
        return

    report = desk.run_once(symbols=active_symbols)
    print(f"[{stamp}] cycle (env={'LIVE' if live else 'demo'}):")
    print(report.describe())
    write_last_cycle(desk, report, stamp)


PAUSE_FLAG = os.path.join(os.path.dirname(__file__), "..", "AQUAFUNDED_PAUSED")


def main() -> None:
    if os.path.exists(PAUSE_FLAG):
        # Session 48 incident: a live short position stacked into 103
        # separate SELL positions instead of closing (close_position's
        # close_quantity=0 silently closed nothing on the position_id
        # path -- see bots/brokers/tradelocker_broker.py's sell()). User
        # closed everything by hand and asked for the bugs fixed and
        # verified BEFORE this trades again. This flag hard-stops the
        # cycle before it touches the broker at all -- delete the
        # AQUAFUNDED_PAUSED file (repo root) to resume once the close
        # path has been re-verified against a real live position.
        print("PAUSED: AQUAFUNDED_PAUSED flag present -- refusing to run a cycle. "
              "Delete that file once the close-position fix is re-verified live.")
        return

    for var in ("TRADELOCKER_EMAIL", "TRADELOCKER_PASSWORD", "TRADELOCKER_SERVER"):
        if not os.environ.get(var):
            print(f"FAIL: {var} is not set -- refusing to start")
            sys.exit(1)

    config = aquafunded_instant_config()
    # Session 48, corrected (confirmed against AquaFunded's own FAQ and
    # TradeLocker's account switcher -- every account this firm issues shows
    # DEMO, permanently, by design): "AquaFunded's Funded Accounts should not
    # be considered live trading accounts... All accounts provided by
    # AquaFunded are demo accounts with virtual funds." There is no live
    # TradeLocker server for this product to ever connect to -- a prior
    # version of this script derived `live` from TRADELOCKER_LIVE and every
    # attempt 401'd, which (correctly, by the if:success() runaway-loop
    # fix) stalled the whole self-chain waiting on the flaky backup cron.
    # Real payouts happen through AquaFunded's own "Request Payout" flow
    # against THIS simulated account, not a different live connection -- so
    # this connector is now hardcoded to the only account that has ever
    # existed for this firm. If AquaFunded ever ships an actual live
    # product, add a NEW connector for it rather than reviving this flag.
    try:
        broker = TradeLockerBroker(live=False)
    except Exception as exc:
        print(f"FAIL: could not connect to TradeLocker: {exc}")
        sys.exit(1)

    # Diagnostic (session 48): the tradelocker library's TLAPI picks
    # WHATEVER ACCOUNT COMES BACK FIRST from this login when no
    # account_id/acc_num is specified -- see TLAPI._set_account_id_and_acc_num.
    # If this login has more than one account (e.g. a newly added account),
    # the bot could silently be connected to a DIFFERENT one than what's
    # showing in the app -- indistinguishable from "the bot can't see my
    # position" without this printed. Always log exactly which account this
    # run is on, and every account this login can see, so a mismatch is
    # visible in the job log instead of a silent mystery.
    try:
        all_accounts = broker.api.get_all_accounts()
        print(f"[account] connected to: {broker.api.account_name!r} "
              f"(id={broker.api.account_id}, accNum={broker.api.acc_num})")
        cols = [c for c in ("id", "accNum", "name", "accountBalance") if c in all_accounts.columns]
        print(f"[account] all accounts visible on this login:\n"
              f"{all_accounts[cols].to_string(index=False)}")
    except Exception as exc:
        print(f"[account] could not list accounts for diagnosis: {exc}")

    desk = TradingDesk(broker=broker, config=config)

    start = time.monotonic()
    checks = 0
    while time.monotonic() - start < LOOP_BUDGET_SECONDS:
        loop_t0 = time.monotonic()
        try:
            run_one_cycle(desk, live=False)
        except Exception as exc:
            # A single bad cycle (transient network blip, broker hiccup)
            # must not kill the whole job -- the loop keeps checking every
            # minute regardless, same as a real always-on process would.
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
