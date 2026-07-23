"""Session 48: is over-trading killing the win rate?

The holdout eval showed the raw agent taking 100-160 trades in a single day
at a ~5% win rate. That pattern (huge trade count, tiny win rate) is the
classic signature of churn -- flipping in/out so often that spread/cost eats
every edge. This tests a concrete fix on UNSEEN days: force a minimum hold
of N bars before an exit is allowed, which mechanically cuts churn. If a
min-hold sharply raises the out-of-sample win rate, that's a real,
deployable finding. If it doesn't, we learn the problem is deeper than churn.

Read-only on the live Q-table (no training, no Claude tokens).

    BOT_DATA_DIR=paper_state PYTHONPATH=. python scripts/overtrading_experiment.py
"""
from __future__ import annotations

import pandas as pd

from bots.learning.agent import QTraderAgent, extract_state
from bots import marketdata

TRAINED_DAYS = {
    "2026-06-22","2026-06-10","2026-06-11","2026-05-18","2026-06-09","2026-06-08",
    "2026-06-25","2026-06-24","2026-07-08","2026-07-06","2026-05-21","2026-05-20",
    "2026-05-15","2026-05-19","2026-06-12","2026-06-05","2026-06-23","2026-07-21",
    "2026-07-07","2026-07-13","2026-05-11","2026-06-18","2026-07-14","2026-07-02",
}
SYMBOLS = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","EURJPY","GBPJPY","GOLD","OIL"]
MIN_HOLDS = [0, 5, 15, 30, 60]  # bars (minutes) an entry must be held before exit allowed
COST = 0.0002  # ~realistic round-trip fraction charged per closed trade for the net-win test


def run(agent, df, min_hold):
    holding = False; entry = 0.0; held = 0
    trades = wins = net_wins = 0
    for i in range(30, len(df) - 1):
        state = extract_state(df, i, holding)
        action = agent.choose_action(state, explore=False)
        price = float(df["close"].iloc[i])
        if action == "buy" and not holding:
            holding = True; entry = price; held = 0
        elif holding:
            held += 1
            if action == "sell" and held >= min_hold:
                ret = (price - entry) / entry
                if price > entry: wins += 1
                if ret > COST: net_wins += 1   # win AFTER costs
                trades += 1; holding = False
    return trades, wins, net_wins


def main():
    frames = {}
    for sym in SYMBOLS:
        try:
            df = marketdata.get_history(marketdata.resolve_symbol(sym), period="8d", interval="1m")
            df.columns = [c.lower() for c in df.columns]
            if isinstance(df.index, pd.DatetimeIndex):
                frames[sym] = df
        except Exception:
            pass
    agent = QTraderAgent(); agent.load()
    print(f"model: {len(agent.q)} states | {len(frames)} symbols\n")

    for mh in MIN_HOLDS:
        T = W = NW = 0
        for sym, df in frames.items():
            days = sorted({d.strftime("%Y-%m-%d") for d in df.index.normalize()})
            for day in [d for d in days if d not in TRAINED_DAYS]:
                d = df[df.index.normalize() == pd.Timestamp(day, tz=df.index.tz)]
                if len(d) > 60:
                    t, w, nw = run(agent, d, mh)
                    T += t; W += w; NW += nw
        if T:
            print(f"min-hold {mh:3d} bars: {T:5d} trades | raw win {W/T*100:4.1f}% | "
                  f"win-after-costs {NW/T*100:4.1f}%")
        else:
            print(f"min-hold {mh:3d} bars: 0 trades (filter suppressed all exits)")


if __name__ == "__main__":
    main()
