"""Tabular Q-learning trading agent.

Learning from mistakes, literally: the agent walks through historical price
data taking actions (buy / sell / hold). Each action's reward is the profit or
loss it produced; Q-values for (market state, action) pairs are updated so that
actions which lost money become less likely to be repeated in that state.

The market state is deliberately coarse (trend x RSI x position) so the agent
generalizes instead of memorizing -- the same design used by the classic
Deep Q-learning bots on GitHub (e.g. pskrunner14/trading-bot), minus the
neural network so it runs anywhere with just numpy/pandas.
"""

from __future__ import annotations

import json
import os
import random
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from bots.paths import data_path

ACTIONS = ("hold", "buy", "sell")


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).rolling(period).mean()
    loss = (-delta.clip(upper=0.0)).rolling(period).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def extract_state(df: pd.DataFrame, index: int, holding: bool) -> str:
    """Discretize the market at bar `index` into a small state string.

    State = trend (fast SMA vs slow SMA) x RSI bucket x whether we hold.
    """
    close = df["close"]
    fast = close.rolling(10).mean()
    slow = close.rolling(30).mean()
    trend = "up" if fast.iloc[index] >= slow.iloc[index] else "down"
    rsi_val = _rsi(close).iloc[index]
    if rsi_val < 35:
        rsi_bucket = "oversold"
    elif rsi_val > 65:
        rsi_bucket = "overbought"
    else:
        rsi_bucket = "neutral"
    pos = "in" if holding else "out"
    return f"trend-{trend}|rsi-{rsi_bucket}|pos-{pos}"


class QTraderAgent:
    def __init__(
        self,
        learning_rate: float = 0.1,
        discount: float = 0.95,
        epsilon: float = 0.1,
        transaction_cost_pct: float = 0.001,
        model_path: Optional[str] = None,
    ):
        self.lr = learning_rate
        self.discount = discount
        self.epsilon = epsilon
        self.cost = transaction_cost_pct
        self.model_path = model_path or data_path("qtable.json")
        self.q: Dict[str, Dict[str, float]] = {}
        self.trained_episodes = 0

    # -- persistence ---------------------------------------------------------

    def save(self, path: Optional[str] = None) -> None:
        path = path or self.model_path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"q": self.q, "episodes": self.trained_episodes}, fh, indent=2)

    def load(self, path: Optional[str] = None) -> bool:
        path = path or self.model_path
        if not os.path.exists(path):
            return False
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.q = data.get("q", {})
        self.trained_episodes = data.get("episodes", 0)
        return True

    # -- core Q-learning ------------------------------------------------------

    def _q_row(self, state: str) -> Dict[str, float]:
        return self.q.setdefault(state, {action: 0.0 for action in ACTIONS})

    def choose_action(self, state: str, explore: bool = False) -> str:
        if explore and random.random() < self.epsilon:
            return random.choice(ACTIONS)
        row = self._q_row(state)
        return max(row, key=row.get)

    def _update(self, state: str, action: str, reward: float, next_state: str) -> None:
        row = self._q_row(state)
        next_best = max(self._q_row(next_state).values())
        row[action] += self.lr * (reward + self.discount * next_best - row[action])

    def train(self, df: pd.DataFrame, episodes: int = 20, warmup: int = 30) -> Dict[str, float]:
        """Run `episodes` passes over the price history, learning as it goes.

        Returns simple stats from the final (greedy) evaluation pass.
        """
        df = _normalize_ohlcv(df)
        for _ in range(episodes):
            self._run_episode(df, warmup, explore=True)
            self.trained_episodes += 1
        return self._run_episode(df, warmup, explore=False)

    def _run_episode(self, df: pd.DataFrame, warmup: int, explore: bool) -> Dict[str, float]:
        holding = False
        entry_price = 0.0
        trades = 0
        wins = 0
        total_return = 0.0
        for i in range(warmup, len(df) - 1):
            state = extract_state(df, i, holding)
            action = self.choose_action(state, explore=explore)
            price = float(df["close"].iloc[i])
            next_price = float(df["close"].iloc[i + 1])

            reward = 0.0
            if action == "buy" and not holding:
                holding = True
                entry_price = price
                reward = -self.cost
            elif action == "sell" and holding:
                trade_return = (price - entry_price) / entry_price - self.cost
                reward = trade_return
                total_return += trade_return
                trades += 1
                if trade_return > 0:
                    wins += 1
                holding = False
            elif holding:
                # mark-to-market reward while in a position
                reward = (next_price - price) / price

            next_state = extract_state(df, i + 1, holding)
            if explore:
                self._update(state, action, reward, next_state)
        return {
            "trades": trades,
            "win_rate": wins / trades if trades else 0.0,
            "total_return_pct": total_return * 100.0,
        }

    # -- inference -------------------------------------------------------------

    def signal(self, df: pd.DataFrame, holding: bool = False) -> str:
        """Action recommendation ('buy' / 'sell' / 'hold') for the latest bar."""
        df = _normalize_ohlcv(df)
        if len(df) < 31:
            return "hold"
        state = extract_state(df, len(df) - 1, holding)
        return self.choose_action(state, explore=False)

    def current_state(self, df: pd.DataFrame, holding: bool = False) -> str:
        df = _normalize_ohlcv(df)
        return extract_state(df, len(df) - 1, holding)


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Accept yfinance-style (Close) or lowercase (close) column names."""
    if "close" in df.columns:
        return df
    renamed = df.rename(columns={c: str(c).lower() for c in df.columns})
    if "close" not in renamed.columns:
        raise ValueError(f"DataFrame needs a close column, got {list(df.columns)}")
    return renamed
