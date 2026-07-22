"""Independent, real-data validation of the challenge pass-probability
estimate in challenge_sim.py.

challenge_sim.py's Monte Carlo trains AND evaluates on the same family of
hand-written synthetic regime generators (bots/learning/scenarios.py) --
disclosed there as a real limitation: part of any measured improvement
could be the agent learning quirks of how those generators are built,
not something that transfers to real markets. This module addresses that
directly by building attempt tapes out of ACTUAL historical 1-minute price
action (fetched via bots.marketdata) instead of synthetic formulas.

Method: block bootstrap. Real intraday history is too short to run
thousands of independent multi-day attempts on directly (a few thousand
bars per pair), so each simulated attempt is built by sampling contiguous
REAL blocks (each block is a real, unbroken stretch of actual price
history -- preserving real short-term volatility clustering and
autocorrelation) and stitching them end to end, price-continuity-adjusted
at each seam. This is a standard technique for exactly this situation
(limited real history, need many simulated paths) -- not synthetic data,
but not one continuous real backtest either. Still not a guarantee.

    python -m bots challenge-odds --real
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from bots.learning.agent import QTraderAgent
from bots.learning.challenge_sim import AttemptResult, simulate_attempt

# Session 47: matches the live desk's ACTUAL --funded launch watchlist
# (see CLAUDE.md), not just a handful of majors -- the estimate should
# reflect what the bot actually trades, including the JPY/other crosses
# and the indices/gold/oil legs, not just 5 pairs.
DEFAULT_PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCHF", "USDCAD",
    "EURJPY", "GBPJPY", "AUDJPY", "EURGBP", "EURCHF",
    "US30", "NAS100", "US500", "US2000", "GOLD", "SILVER", "OIL",
]


def fetch_real_pools(
    symbols: Optional[List[str]] = None, period: str = "7d", interval: str = "1m"
) -> Dict[str, pd.DataFrame]:
    """Real OHLCV history per symbol, straight from bots.marketdata. Symbols
    that fail to fetch (rate limits, outages) are skipped, not fabricated."""
    from bots import marketdata

    pools: Dict[str, pd.DataFrame] = {}
    for raw_sym in symbols or DEFAULT_PAIRS:
        sym = marketdata.resolve_symbol(raw_sym)
        try:
            df = marketdata.get_history(sym, period=period, interval=interval)
            if len(df) > 200:
                pools[raw_sym] = df
        except Exception:
            continue
    return pools


def _block_bootstrap_tape(
    pools: Dict[str, pd.DataFrame],
    rng: np.random.Generator,
    bars: int,
    block_size: int = 120,
) -> pd.DataFrame:
    """One synthetic attempt built from real blocks. Each block is a
    contiguous real slice of ONE symbol's actual price history; blocks are
    chained with a continuity adjustment (each new block's opening price is
    rescaled to pick up exactly where the previous block left off) so the
    combined path has no fake jumps at the seams, while each block's
    internal moves are 100% real, actually-observed price action."""
    symbols = list(pools)
    frames: List[pd.DataFrame] = []
    total = 0
    last_close = None
    while total < bars:
        sym = symbols[rng.integers(0, len(symbols))]
        df = pools[sym]
        max_start = len(df) - block_size
        if max_start <= 0:
            continue
        start = int(rng.integers(0, max_start))
        block = df.iloc[start:start + block_size][["open", "high", "low", "close"]].copy()
        block["volume"] = df.iloc[start:start + block_size].get(
            "volume", pd.Series(0.0, index=block.index)
        ).fillna(0.0).values
        if last_close is not None:
            scale = last_close / float(block["close"].iloc[0])
            block[["open", "high", "low", "close"]] *= scale
        last_close = float(block["close"].iloc[-1])
        frames.append(block)
        total += len(block)

    combined = pd.concat(frames, ignore_index=True).iloc[:bars]
    combined.index = pd.date_range("2025-01-02 09:30", periods=len(combined), freq="1min")
    return combined


def run_real_data_monte_carlo(
    n_attempts: int = 200,
    bars_per_attempt: int = 3000,
    block_size: int = 120,
    agent: Optional[QTraderAgent] = None,
    pools: Optional[Dict[str, pd.DataFrame]] = None,
    symbols: Optional[List[str]] = None,
    seed: int = 0,
    **rule_kwargs,
) -> Dict[str, object]:
    if agent is None:
        agent = QTraderAgent()
        agent.load()
    if pools is None:
        pools = fetch_real_pools(symbols)
    if not pools:
        raise RuntimeError("no real market data available -- check network/marketdata access")

    outcomes = {"pass": 0, "fail": 0, "undecided": 0}
    gains: List[float] = []
    for i in range(n_attempts):
        rng = np.random.default_rng(seed * 7_919 + i)
        df = _block_bootstrap_tape(pools, rng, bars_per_attempt, block_size)
        result: AttemptResult = simulate_attempt(agent, df, **rule_kwargs)
        outcomes[result.outcome] += 1
        gains.append(result.final_gain_pct)

    n = max(n_attempts, 1)
    return {
        "attempts": n_attempts,
        "symbols_used": sorted(pools),
        "pass_rate": outcomes["pass"] / n,
        "fail_rate": outcomes["fail"] / n,
        "undecided_rate": outcomes["undecided"] / n,
        "avg_final_gain_pct": float(np.mean(gains)) * 100.0 if gains else 0.0,
    }
