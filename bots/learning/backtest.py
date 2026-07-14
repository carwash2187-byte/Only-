"""Out-of-sample backtest: the honest way to test a trading bot.

Splits history into a training window and a held-out test window the agent
has NEVER seen, trains a fresh agent on the first part only, then measures
it on the unseen part against buy-and-hold. In-sample results (testing on
the data you trained on) always flatter the bot; this is the number that
actually predicts anything. Same walk-forward principle the serious GitHub
frameworks use (freqtrade backtesting, vectorbt, backtesting.py).

    python -m bots backtest --symbol SPY --period 3mo --interval 5m
"""

from __future__ import annotations

from typing import Dict

import pandas as pd

from bots.learning.agent import QTraderAgent, _normalize_ohlcv


def run_backtest(
    df: pd.DataFrame,
    train_fraction: float = 0.7,
    episodes: int = 40,
    warmup: int = 30,
) -> Dict[str, float]:
    df = _normalize_ohlcv(df)
    if len(df) < warmup * 4:
        raise ValueError(f"Not enough data to backtest ({len(df)} bars)")
    split = int(len(df) * train_fraction)
    train_df = df.iloc[:split]
    test_df = df.iloc[split:].copy()  # copy -> feature cache stays separate

    agent = QTraderAgent(model_path="/dev/null")  # fresh, never persisted
    agent.train(train_df, episodes=episodes, warmup=warmup)
    result = agent._run_episode(test_df, warmup=warmup, explore=False)

    start = float(test_df["close"].iloc[warmup])
    end = float(test_df["close"].iloc[-1])
    buy_hold_pct = (end / start - 1.0) * 100.0
    return {
        "train_bars": len(train_df),
        "test_bars": len(test_df),
        "trades": result["trades"],
        "win_rate": result["win_rate"],
        "agent_return_pct": result["total_return_pct"],
        "buy_hold_return_pct": buy_hold_pct,
        "edge_vs_buy_hold_pct": result["total_return_pct"] - buy_hold_pct,
    }


def main() -> None:
    import argparse

    from bots.learning.train import fetch_history

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--period", default="3mo")
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--episodes", type=int, default=40)
    args = parser.parse_args()

    df = fetch_history(args.symbol, args.period, args.interval)
    r = run_backtest(df, episodes=args.episodes)
    print(
        f"Out-of-sample backtest {args.symbol} ({args.interval}): trained on "
        f"{r['train_bars']} bars, tested on {r['test_bars']} UNSEEN bars"
    )
    print(
        f"  agent: {r['trades']} trades, win rate {r['win_rate']:.0%}, "
        f"return {r['agent_return_pct']:+.2f}%"
    )
    print(f"  buy-and-hold same window: {r['buy_hold_return_pct']:+.2f}%")
    verdict = "BEAT" if r["edge_vs_buy_hold_pct"] > 0 else "LOST TO"
    print(f"  -> agent {verdict} buy-and-hold by {abs(r['edge_vs_buy_hold_pct']):.2f}%")


if __name__ == "__main__":
    main()
