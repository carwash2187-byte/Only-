"""Session 48: honest, out-of-sample win rate check.

The self-train practice run's own printed "win rate" is measured on the
SAME historical windows the agent just trained on (train() runs explore
passes, then one final explore=False pass on that identical data) -- a
real number, but not the number that answers "how good is this on data
it hasn't seen." This script answers that question directly: load the
LIVE qtable.json (no training here, explore=False is read-only -- see
agent.py's `if explore: self._update(...)`) and run it against real
recent days that are NOT in the practice run's training window list.

    BOT_DATA_DIR=paper_state PYTHONPATH=. python scripts/holdout_eval.py
"""
from __future__ import annotations

import pandas as pd

from bots.learning.agent import QTraderAgent
from bots import marketdata

# Dates the manual practice run (stress_test.py --practice, session 48)
# actually trained on -- pulled directly from its log, not guessed.
TRAINED_DAYS = {
    "2026-06-22", "2026-06-10", "2026-06-11", "2026-05-18", "2026-06-09",
    "2026-06-08", "2026-06-25", "2026-06-24", "2026-07-08", "2026-07-06",
    "2026-05-21", "2026-05-20", "2026-05-15", "2026-05-19", "2026-06-12",
    "2026-06-05", "2026-06-23", "2026-07-21", "2026-07-07", "2026-07-13",
    "2026-05-11", "2026-06-18", "2026-07-14", "2026-07-02",
}

SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD",
    "EURJPY", "GBPJPY", "GOLD", "OIL",
]


def main() -> None:
    agent = QTraderAgent()
    loaded = agent.load()
    print(f"live model: {'loaded' if loaded else 'FRESH'} ({len(agent.q)} states)")

    total_trades = 0
    total_wins = 0
    per_symbol = []
    for sym in SYMBOLS:
        try:
            real_sym = marketdata.resolve_symbol(sym)
            df = marketdata.get_history(real_sym, period="8d", interval="1m")
        except Exception as exc:
            print(f"  {sym}: fetch failed ({exc})")
            continue
        df.columns = [c.lower() for c in df.columns]
        if not isinstance(df.index, pd.DatetimeIndex):
            continue
        days = sorted({d.strftime("%Y-%m-%d") for d in df.index.normalize()})
        holdout_days = [d for d in days if d not in TRAINED_DAYS]
        for day in holdout_days:
            day_df = df[df.index.normalize() == pd.Timestamp(day, tz=df.index.tz)]
            if len(day_df) < 60:
                continue
            stats = agent._run_episode(day_df, warmup=30, explore=False)
            if stats["trades"] > 0:
                wins = round(stats["win_rate"] * stats["trades"])
                total_trades += stats["trades"]
                total_wins += wins
                per_symbol.append((sym, day, stats["trades"], stats["win_rate"]))
                print(f"  {sym} {day}: {stats['trades']} trades, "
                      f"win rate {stats['win_rate']*100:.0f}%, "
                      f"return {stats['total_return_pct']:+.2f}%")

    print()
    if total_trades:
        print(f"HOLDOUT (never trained on) TOTAL: {total_trades} trades, "
              f"real win rate {total_wins/total_trades*100:.1f}%")
    else:
        print("No holdout trades executed -- model held flat on all untrained days.")


if __name__ == "__main__":
    main()
