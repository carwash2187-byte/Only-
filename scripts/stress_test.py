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
import sys
import tempfile

import pandas as pd

from bots import marketdata
from bots.brokers import PaperBroker
from bots.journal import TradeJournal
from bots.learning import QTraderAgent
from bots.organization import DrawdownGuard, MaxDrawdownGuard, TradingDesk, funded_account_config

# The FULL live watchlist (same 17 instruments the autopilot actually
# trades) -- earlier sessions practiced on only 8 of them, meaning the 9
# forex crosses the desk trades live had never been seen in training.
WATCHLIST = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCHF", "USDCAD",
    "EURJPY", "GBPJPY", "AUDJPY", "EURGBP", "EURCHF",
    "YM=F", "NQ=F", "ES=F", "RTY=F", "GC=F", "SI=F", "CL=F",
]


def find_roughest_days(dfs: dict, n: int = 1) -> list:
    """The n real calendar days with the highest combined intraday range
    across the whole watchlist -- the closest thing to 'the market was
    bad' this free, ~60-day intraday data can actually show."""
    combined = None
    for df in dfs.values():
        ranges = (df["high"] - df["low"]) / df["close"]
        daily = ranges.groupby(df.index.normalize()).sum()
        combined = daily if combined is None else combined.add(daily, fill_value=0.0)
    return list(combined.nlargest(n).index)


def find_best_trend_days(dfs: dict, n: int = 1) -> list:
    """The n real days with the strongest combined directional move across
    the watchlist -- the big-runner days a trend-following scalper actually
    gets paid on (large net move, not just large chop). Practicing on both
    these AND the roughest days gives the agent reps in the two regimes
    that matter: the days that can hurt it and the days it must not waste."""
    combined = None
    for df in dfs.values():
        daily_close = df["close"].groupby(df.index.normalize())
        net_move_pct = (daily_close.last() - daily_close.first()).abs() / daily_close.first()
        combined = net_move_pct if combined is None else combined.add(net_move_pct, fill_value=0.0)
    return list(combined.nlargest(n).index)


def _fetch_full_history() -> dict:
    print("Fetching real history for the live watchlist...")
    full = {}
    for sym in WATCHLIST:
        try:
            full[sym] = marketdata.get_history(sym, period="60d", interval="5m")
        except Exception as exc:
            print(f"  {sym}: unavailable ({exc}), skipping")
    return full


def _window_around(full: dict, day: pd.Timestamp) -> dict:
    window_start = day - pd.Timedelta(days=4)
    window_end = day + pd.Timedelta(days=2)
    dfs = {s: df[(df.index >= window_start) & (df.index < window_end)].copy()
           for s, df in full.items()}
    return {s: df for s, df in dfs.items() if len(df) > 40}


def fetch_rough_window() -> dict:
    full = _fetch_full_history()
    roughest = find_roughest_days(full, 1)[0]
    print(f"roughest real window found (combined across the watchlist): {roughest.date()}")
    return _window_around(full, roughest)


def practice_on_rough_windows(episodes: int = 20, windows: int = 3) -> None:
    """Train the LIVE Q-agent (respects BOT_DATA_DIR, same model_path the
    autopilot loads) on the N real roughest windows found -- genuine
    practice on real historical hard conditions, using the exact same
    agent.train() mechanism the daily SPY/NVDA training already uses
    safely. This only ever updates the Q-table; it never opens/closes a
    TradeRecord, so it cannot touch the journal, the win-rate/
    profit-factor numbers, or anything the funded-account-readiness
    question is graded on -- those stay 100% real trades only."""
    full = _fetch_full_history()
    rough_days = find_roughest_days(full, windows)
    trend_days = [d for d in find_best_trend_days(full, windows) if d not in rough_days]
    print(f"top {len(rough_days)} roughest real days across the watchlist: "
          + ", ".join(str(d.date()) for d in rough_days))
    print(f"top {len(trend_days)} strongest-trend real days (the 'big runner' regime): "
          + ", ".join(str(d.date()) for d in trend_days))
    rough_days = rough_days + trend_days

    agent = QTraderAgent()  # default model_path -> respects BOT_DATA_DIR
    loaded = agent.load()
    before_states = len(agent.q)
    print(f"live model: {'loaded existing table' if loaded else 'starting fresh'} "
          f"({before_states} known states)")
    for day in rough_days:
        dfs = _window_around(full, day)
        print(f"\n-- practicing on the window around {day.date()} --")
        for sym, df in dfs.items():
            if len(df) < 120:
                continue
            stats = agent.train(df, episodes=episodes)
            print(f"  {sym} ({len(df)} bars x {episodes} episodes): "
                  f"{stats['trades']} eval trades, win rate {stats['win_rate']:.0%}, "
                  f"return {stats['total_return_pct']:+.1f}%")
    agent.save()
    print(f"\nsaved -- live model now knows {len(agent.q)} states "
          f"({len(agent.q) - before_states:+d} vs before). "
          "Restart the live autopilot process to pick this up "
          "(it loaded its agent at startup, in memory).")


def practice_deep_history(episodes: int = 6) -> None:
    """The closest real thing to 'years of experience overnight': train the
    live Q-agent on ~2 YEARS of hourly bars (Yahoo's 1h limit is 730 days)
    across the whole watchlist -- every real volatility event, trend run,
    and dead zone in that window, replayed with learning on. Roughly 10x
    the bars of the 60-day intraday practice mode. Same guarantees: only
    the Q-table is updated; the journal/track record is never touched."""
    agent = QTraderAgent()  # default model_path -> respects BOT_DATA_DIR
    loaded = agent.load()
    before = len(agent.q)
    print(f"live model: {'loaded existing table' if loaded else 'starting fresh'} "
          f"({before} known states)")
    for sym in WATCHLIST:
        try:
            df = marketdata.get_history(sym, period="730d", interval="1h")
        except Exception as exc:
            print(f"  {sym}: unavailable ({exc}), skipping")
            continue
        if len(df) < 500:
            print(f"  {sym}: only {len(df)} hourly bars, skipping")
            continue
        stats = agent.train(df, episodes=episodes)
        print(f"  {sym} ({len(df)} hourly bars x {episodes} episodes, "
              f"{df.index[0].date()} -> {df.index[-1].date()}): "
              f"{stats['trades']} eval trades, win rate {stats['win_rate']:.0%}, "
              f"return {stats['total_return_pct']:+.1f}%")
    agent.save()
    print(f"\nsaved -- live model now knows {len(agent.q)} states "
          f"({len(agent.q) - before:+d} vs before). Restart the live autopilot "
          "to pick this up.")


def stress_test(starting_cash: float = 5_000.0) -> None:
    dfs = fetch_rough_window()
    master_index = sorted(set().union(*[set(df.index) for df in dfs.values()]))
    print(f"replaying {master_index[0]} to {master_index[-1]} "
          f"({len(master_index)} timestamps, {len(dfs)} symbols, real 5-minute data)")

    tmp = tempfile.mkdtemp(prefix="stress_test_")
    try:
        broker = PaperBroker(starting_cash=starting_cash, state_path=f"{tmp}/acct.json")
        journal = TradeJournal(path=f"{tmp}/journal.json")
        guard = DrawdownGuard(state_path=f"{tmp}/day.json")
        dd_guard = MaxDrawdownGuard(state_path=f"{tmp}/dd.json")
        # wall-clock-based guards off in a replay: the news calendar is live
        # network data, and the rollover/Friday-close windows key off REAL
        # time, not the historical bar being replayed
        config = funded_account_config(
            news_blackout=False, rollover_blackout=False, friday_flatten=False
        )
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
    if "--practice-deep" in sys.argv:
        practice_deep_history()
    elif "--practice" in sys.argv:
        practice_on_rough_windows()
    else:
        stress_test()
