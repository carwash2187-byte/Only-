"""Session 48: real train/test experiment to try to raise win rate.

Honest method: split real recent 1m data into TRAIN days and TEST (unseen)
days. Train a fresh Q-agent (current code, incl. the new momentum feature)
on TRAIN days only, then measure win rate on TEST days for two policies:

  A) BASELINE: the agent acts on any state (current live behavior).
  B) SELECTIVE: only ENTER when momentum agrees with trend (mom-up & trend-up
     for longs). Thesis (CLAUDE.md's own "quality over quantity" principle):
     fewer, higher-conviction trades -> higher win rate. This is a real,
     testable hypothesis, not a guaranteed win -- the point is to MEASURE it
     on unseen data, then keep it only if the evidence supports it.

No Claude tokens involved in the compute. Prints a heartbeat per symbol.

    BOT_DATA_DIR=paper_state PYTHONPATH=. python scripts/improve_experiment.py
"""
from __future__ import annotations

import pandas as pd

from bots.learning.agent import QTraderAgent, extract_state
from bots import marketdata

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURJPY", "GOLD", "OIL"]
EPISODES = 40
WARMUP = 30


def _run_policy(agent, df, selective: bool):
    """Read-only walk over df; returns (trades, wins). If selective, only
    open a long when momentum & trend both point up at entry."""
    holding = False
    entry = 0.0
    trades = wins = 0
    for i in range(WARMUP, len(df) - 1):
        state = extract_state(df, i, holding)
        action = agent.choose_action(state, explore=False)
        price = float(df["close"].iloc[i])
        if action == "buy" and not holding:
            if selective and not ("mom-up" in state and "trend-up" in state):
                continue
            holding = True
            entry = price
        elif action == "sell" and holding:
            if price > entry:
                wins += 1
            trades += 1
            holding = False
    return trades, wins


def main() -> None:
    frames = {}
    for sym in SYMBOLS:
        try:
            real = marketdata.resolve_symbol(sym)
            df = marketdata.get_history(real, period="8d", interval="1m")
            df.columns = [c.lower() for c in df.columns]
            if isinstance(df.index, pd.DatetimeIndex) and len(df) > 200:
                frames[sym] = df
        except Exception as exc:
            print(f"  {sym}: fetch failed ({exc})")
    print(f"fetched {len(frames)} symbols")

    base_t = base_w = sel_t = sel_w = 0
    for sym, df in frames.items():
        days = sorted({d.strftime("%Y-%m-%d") for d in df.index.normalize()})
        if len(days) < 4:
            continue
        split = int(len(days) * 0.6)
        train_days, test_days = set(days[:split]), set(days[split:])

        agent = QTraderAgent(model_path="/dev/null")  # fresh, isolated
        for day in train_days:
            d = df[df.index.normalize() == pd.Timestamp(day, tz=df.index.tz)]
            if len(d) > 60:
                agent.train(d, episodes=EPISODES, warmup=WARMUP)

        for day in test_days:
            d = df[df.index.normalize() == pd.Timestamp(day, tz=df.index.tz)]
            if len(d) <= 60:
                continue
            bt, bw = _run_policy(agent, d, selective=False)
            st, sw = _run_policy(agent, d, selective=True)
            base_t += bt; base_w += bw; sel_t += st; sel_w += sw
        print(f"  {sym}: trained {len(train_days)}d, tested {len(test_days)}d "
              f"| baseline so far {base_w}/{base_t}, selective {sel_w}/{sel_t}", flush=True)

    print("\n=== HONEST OUT-OF-SAMPLE RESULT (unseen test days) ===")
    if base_t:
        print(f"BASELINE  : {base_t} trades, win rate {base_w/base_t*100:.1f}%")
    else:
        print("BASELINE  : 0 trades")
    if sel_t:
        print(f"SELECTIVE : {sel_t} trades, win rate {sel_w/sel_t*100:.1f}%")
    else:
        print("SELECTIVE : 0 trades (filter too strict on this data)")
    if base_t and sel_t:
        delta = sel_w/sel_t*100 - base_w/base_t*100
        print(f"\nSelectivity filter changed win rate by {delta:+.1f} points "
              f"({'KEEP -- real improvement' if delta > 2 else 'DO NOT keep -- no real gain'}).")


if __name__ == "__main__":
    main()
