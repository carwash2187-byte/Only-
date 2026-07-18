"""Preflight check for connecting a (demo or funded) TradeLocker account.

Run this BEFORE ever starting the autopilot against a new account. It is
read-only by default: it connects, reads the account, resolves every
watchlist symbol to this firm's instrument names, and fetches a live price
for each -- proving the whole pipeline works without placing any order.

Usage (one account at a time -- for two accounts, run it twice with each
account's env vars):

    TRADELOCKER_EMAIL=... TRADELOCKER_PASSWORD=... TRADELOCKER_SERVER=... \
        PYTHONPATH=. python scripts/preflight_funded.py

Optional end-to-end order test (DEMO ONLY -- refuses to run on live):

    ... python scripts/preflight_funded.py --order-test

That places the smallest possible EURUSD bracket order (with server-side
stop-loss and take-profit attached), confirms the position shows up under
the desk's symbol name, closes it through the position endpoint, and
confirms it is gone. If that passes on the firm's demo server, the same
code path is what the live desk will use.
"""

from __future__ import annotations

import os
import sys
import time

WATCHLIST = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCHF", "USDCAD",
    "EURJPY", "GBPJPY", "AUDJPY", "EURGBP", "EURCHF",
    "US30", "NAS100", "US500", "GOLD", "OIL",
]


def main() -> int:
    for var in ("TRADELOCKER_EMAIL", "TRADELOCKER_PASSWORD", "TRADELOCKER_SERVER"):
        if not os.environ.get(var):
            print(f"FAIL: {var} is not set")
            return 1

    from bots.brokers.tradelocker_broker import TradeLockerBroker

    live = os.environ.get("TRADELOCKER_LIVE") == "1"
    print(f"environment: {'LIVE (real money)' if live else 'demo'}")
    print(f"server: {os.environ['TRADELOCKER_SERVER']}")
    print("connecting...")
    try:
        broker = TradeLockerBroker()
    except Exception as exc:
        print(f"FAIL: could not connect/authenticate: {exc}")
        return 1
    print("connected.")

    ok = True
    try:
        cash = broker.cash()
        equity = broker.equity()
        print(f"cash/available: {cash:,.2f}   equity: {equity:,.2f}")
        if equity <= 0:
            print("WARN: equity is zero -- is this the right account?")
    except Exception as exc:
        print(f"FAIL: could not read account state: {exc}")
        ok = False

    try:
        positions = broker.positions()
        print(f"open positions: {positions or 'none'}")
        if positions:
            print("WARN: account already has open positions -- the desk will "
                  "manage/reconcile them; make sure that's expected")
    except Exception as exc:
        print(f"FAIL: could not read positions: {exc}")
        ok = False

    print("\nresolving watchlist symbols on this account:")
    resolved = 0
    for symbol in WATCHLIST:
        try:
            iid = broker._instrument_id(symbol)
            tl_name = broker.api.get_symbol_name_from_instrument_id(iid)
            price = broker.price(symbol)
            if price <= 0:
                raise ValueError(f"price came back {price}")
            min_qty = ""
            try:
                details = broker.api.get_instrument_details(iid)
                for key in ("minTradeQty", "minOrderQty", "lotStep", "minLot"):
                    if key in details:
                        min_qty = f"  min qty {details[key]}"
                        break
            except Exception:
                pass
            print(f"  {symbol:8s} -> {tl_name} (id {iid})  ask {price}{min_qty}")
            resolved += 1
        except Exception as exc:
            print(f"  {symbol:8s} -> UNRESOLVED: {exc}")
            ok = False
    print(f"{resolved}/{len(WATCHLIST)} symbols tradeable on this account")

    if "--order-test" in sys.argv:
        if live:
            print("\nFAIL: --order-test refuses to run against a LIVE account. "
                  "Run it on the firm's demo server first.")
            return 1
        print("\norder test (demo): smallest EURUSD bracket order...")
        result = broker.buy_bracket("EURUSD", 1_000, 0.005, 0.01)
        if not result.ok:
            print(f"FAIL: bracket order rejected: {result.error}")
            return 1
        print(f"  bracket order accepted (id {result.order_id}), waiting for fill...")
        time.sleep(3)
        pos = broker.positions()
        if pos.get("EURUSD", 0) <= 0:
            print(f"FAIL: position did not appear under desk name; positions={pos}")
            return 1
        print(f"  position visible under desk name: EURUSD={pos['EURUSD']}")
        close = broker.sell("EURUSD", 1_000)
        if not close.ok:
            print(f"FAIL: could not close test position: {close.error} -- "
                  "CLOSE IT MANUALLY in the TradeLocker app before proceeding")
            return 1
        time.sleep(3)
        pos = broker.positions()
        if pos.get("EURUSD", 0) > 0:
            print(f"FAIL: position still open after close: {pos} -- "
                  "CLOSE IT MANUALLY in the TradeLocker app")
            return 1
        print("  closed cleanly. full order round-trip works.")

    print("\n" + ("PREFLIGHT PASSED" if ok else "PREFLIGHT FAILED -- fix the FAIL lines above"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
