"""Train the Q-learning agent on real historical data via yfinance.

Usage:
    python -m bots.learning.train --symbol AAPL --period 2y --episodes 50
"""

from __future__ import annotations

import argparse

from bots.learning.agent import QTraderAgent


def fetch_history(symbol: str, period: str = "2y", interval: str = "1d"):
    import yfinance as yf

    df = yf.Ticker(symbol).history(period=period, interval=interval)
    if df.empty:
        raise SystemExit(f"No data returned for {symbol}")
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--period", default="2y")
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--episodes", type=int, default=50)
    args = parser.parse_args()

    df = fetch_history(args.symbol, args.period, args.interval)
    agent = QTraderAgent()
    agent.load()  # continue from previous training if a model exists
    stats = agent.train(df, episodes=args.episodes)
    agent.save()
    print(f"Trained {args.episodes} episodes on {args.symbol} ({len(df)} bars).")
    print(
        f"Greedy evaluation: {stats['trades']} trades, "
        f"win rate {stats['win_rate']:.0%}, total return {stats['total_return_pct']:+.1f}%"
    )
    print(f"Model saved. Latest signal for {args.symbol}: {agent.signal(df)}")


if __name__ == "__main__":
    main()
