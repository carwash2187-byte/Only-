"""Replay the live desk's exact rules against real historical price data,
across the SAME multi-symbol watchlist it actually trades live, looking
for the roughest stretch available -- to answer one question: would the
funded-account safety limits (3% daily loss, 5% max drawdown) have
actually caught a bad run, or just look good on paper?

This is NOT a live trade and never touches paper_state/ -- it runs in a
throwaway temp directory so it can never contaminate the real paper
account's journal or track record. It replays real market history bar by
bar (each cycle only sees data up to that point -- no lookahead), feeding
it through the same TradingDesk class, funded_account_config(), and
correlation/session/regime filters the live autopilot uses, with ALL
symbols live each cycle (a single-symbol replay understates real trade
frequency -- the live desk always has the whole watchlist to choose from).

Honest limitation: free intraday (1m/5m) history only goes back ~60 days
(Yahoo's limit) -- this cannot replay a multi-year-old crash minute by
minute. It finds and stress-tests the roughest *real* window available
within that data.

Usage: PYTHONPATH=. python scripts/stress_test.py
"""

from __future__ import annotations

import shutil
import tempfile

import pandas as pd

from bots import marketdata
from bots.brokers import PaperBroker
from bots.journal import TradeJournal
from bots.learning import QTraderAgent
from bots.organization import DrawdownGuard, MaxDrawdownGuard, TradingDesk, funded_account_config

WATCHLIST = ["EURUSD", "GBPUSD", "USDJPY", "YM=F", "NQ=F", "ES=F", "GC=F", "CL=F"]


def find_roughest_window(dfs: dict) -> pd.Timestamp:
    """The real calendar day with the highest combined intraday range
    across the whole watchlist -- the closest thing to 'the market was
    bad' this free, ~60-day intraday data can actually show."""
    combined = None
    for df in dfs.values():
        ranges = (df["high"] - df["low"]) / df["close"]
        daily = ranges.groupby(df.index.normalize()).sum()
        combined = daily if combined is None else combined.add(daily, fill_value=0.0)
    return combined.idxmax()


def stress_test(starting_cash: float = 5_000.0) -> None:
    print("Fetching real history for the live watchlist...")
    full = {}
    for sym in WATCHLIST:
        try:
            full[sym] = marketdata.get_history(sym, period="60d", interval="5m")
        except Exception as exc:
            print(f"  {sym}: unavailable ({exc}), skipping")
    roughest = find_roughest_window(full)
    print(f"roughest real window found (combined across the watchlist): {roughest.date()}")

    window_start = roughest - pd.Timedelta(days=4)
    window_end = roughest + pd.Timedelta(days=2)
    dfs = {s: df[(df.index >= window_start) & (df.index < window_end)].copy()
           for s, df in full.items()}
    dfs = {s: df for s, df in dfs.items() if len(df) > 40}
    master_index = sorted(set().union(*[set(df.index) for df in dfs.values()]))
    print(f"replaying {master_index[0]} to {master_index[-1]} "
          f"({len(master_index)} timestamps, {len(dfs)} symbols, real 5-minute data)")

    tmp = tempfile.mkdtemp(prefix="stress_test_")
    try:
        broker = PaperBroker(starting_cash=starting_cash, state_path=f"{tmp}/acct.json")
        journal = TradeJournal(path=f"{tmp}/journal.json")
        guard = DrawdownGuard(state_path=f"{tmp}/day.json")
        dd_guard = MaxDrawdownGuard(state_path=f"{tmp}/dd.json")
        config = funded_account_config(news_blackout=False)  # no live network calendar in a sim
        agent = QTraderAgent(model_path=f"{tmp}/q.json")

        state = {"ts": master_index[0]}

        def _sliced(symbol: str) -> pd.DataFrame:
            df = dfs[symbol]
            return df[df.index <= state["ts"]]

        def history_fn(symbol: str) -> pd.DataFrame:
            return _sliced(symbol)

        def htf_history_fn(symbol: str, _timeframe: str) -> pd.DataFrame:
            return _sliced(symbol).resample("1h").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last"}
            ).dropna()

        desk = TradingDesk(
            broker=broker, journal=journal, agent=agent, config=config,
            history_fn=history_fn, guard=guard, max_drawdown_guard=dd_guard,
            manual_signals_path=f"{tmp}/manual.json",
            htf_history_fn=htf_history_fn,
        )

        equity_curve = []
        halts = []
        warmup_ts = master_index[min(60, len(master_index) - 1)]
        for ts in master_index:
            state["ts"] = ts
            if ts < warmup_ts:
                continue
            for sym, df in dfs.items():
                have = df[df.index <= ts]
                if len(have):
                    broker.price_overrides[sym] = float(have["close"].iloc[-1])
            try:
                report = desk.run_once(symbols=list(dfs.keys()))
            except Exception as exc:
                print(f"  cycle {ts} failed (skipped): {exc}")
                continue
            equity_curve.append((ts, broker.equity()))
            for note in report.notes:
                if "HALTED" in note or "CIRCUIT BREAKER" in note:
                    halts.append((ts, note))

        peak = max(e for _, e in equity_curve) if equity_curve else starting_cash
        trough = min(e for _, e in equity_curve) if equity_curve else starting_cash
        final = equity_curve[-1][1] if equity_curve else starting_cash
        worst_dd = (1.0 - trough / peak) * 100.0 if peak > 0 else 0.0

        print(f"\nstart ${starting_cash:,.2f} -> end ${final:,.2f} "
              f"({(final/starting_cash-1)*100:+.2f}%)")
        print(f"worst peak-to-trough dip over the whole replay: {worst_dd:.2f}%")
        print(f"trades taken: {len(journal.trades)}")
        if halts:
            print(f"safety limit triggered {len(halts)} time(s):")
            seen = set()
            for ts, note in halts:
                key = note.split(":")[0]
                if key in seen:
                    continue
                seen.add(key)
                print(f"  [{ts}] {note}")
        else:
            print("safety limits never triggered during this replay "
                  "(the desk's own entry discipline kept it out of the worst of it, "
                  "or the real window available just wasn't rough enough to test the wall)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    stress_test()
