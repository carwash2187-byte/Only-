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


class _FeatureCache:
    """Plain holder so the cached frame never participates in pandas'
    attrs-equality checks (object identity compares to a clean bool)."""

    __slots__ = ("frame",)

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame


def _is_intraday(df: pd.DataFrame) -> bool:
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex) or len(idx) < 3:
        return False
    spacing = pd.Series(idx[1:] - idx[:-1]).median()
    return spacing < pd.Timedelta(hours=6)


def _feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Per-bar discretized features, computed once per DataFrame and cached
    on it (training walks the same frame thousands of times).

    Daily bars keep the original trend+RSI features so the existing Q-table
    stays valid. Intraday bars add the day-trading features the evidence
    supports (see docs/DAY-TRADING-NOTES.md): session VWAP side, opening
    range position, and session phase.
    """
    # The cache is wrapped in a plain holder object rather than stored as a
    # bare DataFrame: pandas compares df.attrs dicts during concat/finalize,
    # and a DataFrame value there raises "truth value is ambiguous" in ANY
    # later pd.concat involving this frame.
    cached = df.attrs.get("_qstate_features")
    if isinstance(cached, _FeatureCache) and len(cached.frame) == len(df):
        return cached.frame

    close = df["close"]
    fast = close.rolling(10).mean()
    slow = close.rolling(30).mean()
    rsi = _rsi(close)
    features = pd.DataFrame(index=df.index)
    features["trend"] = np.where(fast >= slow, "up", "down")
    features["rsi"] = np.select(
        [rsi < 35, rsi > 65], ["oversold", "overbought"], default="neutral"
    )

    if _is_intraday(df):
        high = df["high"] if "high" in df.columns else close
        low = df["low"] if "low" in df.columns else close
        volume = df["volume"] if "volume" in df.columns else pd.Series(0.0, index=df.index)
        volume = volume.fillna(0.0)

        session = df.index.normalize()
        bar_minutes = max(
            1, int(pd.Series(df.index[1:] - df.index[:-1]).median().total_seconds() // 60)
        )
        bars_30min = max(1, round(30 / bar_minutes))
        bars_1h = max(1, round(60 / bar_minutes))
        grouped = features.groupby(session, sort=False)
        bar_of_day = grouped.cumcount()
        bars_in_day = features.groupby(session, sort=False)["trend"].transform("size")

        # Session VWAP (typical price weighted by volume); zero-volume feeds
        # (e.g. Yahoo forex) fall back to the session's running average price.
        typical = (high + low + close) / 3.0
        cum_vol = volume.groupby(session).cumsum()
        cum_pv = (typical * volume).groupby(session).cumsum()
        anchor = np.where(
            cum_vol > 0,
            cum_pv / cum_vol.replace(0.0, np.nan),
            typical.groupby(session).expanding().mean().reset_index(level=0, drop=True),
        )
        features["vwap"] = np.where(close.values >= anchor, "above", "below")

        # Opening range: high/low of the first 30 minutes of each session.
        in_or_window = bar_of_day < bars_30min
        or_high = high.where(in_or_window).groupby(session).transform("max")
        or_low = low.where(in_or_window).groupby(session).transform("min")
        features["orb"] = np.select(
            [in_or_window, close > or_high, close < or_low],
            ["in", "up", "down"],
            default="in",
        )

        # Session phase: opening hour / midday / final hour.
        features["tod"] = np.select(
            [bar_of_day < bars_1h, bar_of_day >= (bars_in_day - bars_1h)],
            ["open", "late"],
            default="mid",
        )

    df.attrs["_qstate_features"] = _FeatureCache(features)
    return features


def extract_state(df: pd.DataFrame, index: int, holding: bool) -> str:
    """Discretize the market at bar `index` into a small state string.

    Daily candles: trend x RSI x position (unchanged from v1, so trained
    daily states keep working). Intraday candles additionally carry VWAP
    side, opening-range position, and session phase.
    """
    row = _feature_frame(df).iloc[index]
    state = f"trend-{row['trend']}|rsi-{row['rsi']}"
    if "vwap" in row.index:
        state += f"|vwap-{row['vwap']}|orb-{row['orb']}|tod-{row['tod']}"
    return state + f"|pos-{'in' if holding else 'out'}"


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
