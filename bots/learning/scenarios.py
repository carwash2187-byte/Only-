"""Synthetic market-regime practice + stress harness.

Real price history here is thin on the *rare* regimes that hurt a scalping
desk the most: flash crashes, news whipsaws, gap opens, parabolic blow-off
tops, and long grinding chop. This module manufactures those regimes on
demand -- hundreds of labelled synthetic sessions -- so the Q-learning agent
can *practice* on them and, just as importantly, so we can *measure* where
the current policy behaves badly.

Two honest uses, and one thing this is NOT:

1. **Practice (data augmentation).** Training the agent across many regime
   types hardens its policy against conditions the real data barely shows.
   This is a legitimate, standard technique -- but synthetic returns are
   NOT a track record. Grade the desk from the real journal, never from
   made-up P&L (project honesty norm).

2. **Stress test / regression.** The per-regime report answers "does the
   policy lose catastrophically in a flash crash or a news spike?" That is
   the genuinely valuable output: it surfaces the regimes to defend against,
   independent of whether we keep the practised weights.

3. **NOT a profit oracle and NOT a journal writer.** `run_practice` never
   touches the real trade journal or paper account -- it only trains the
   Q-table (a policy), and only saves it when the caller explicitly opts in.

    python -m bots practice --scenarios 300           # report only, fresh agent
    python -m bots practice --scenarios 300 --save     # also harden the live Q-table
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from bots.learning.agent import QTraderAgent

# ---------------------------------------------------------------------------
# Regime price-path generators. Each returns a 1-D array of close prices of
# length `n`, expressed as a walk away from `start`. They use percentage moves
# so the absolute price level is irrelevant. `rng` makes every variant of the
# same regime different but reproducible.
# ---------------------------------------------------------------------------

def _walk(start: float, rets: np.ndarray) -> np.ndarray:
    """Compound a series of per-bar returns into a price path."""
    return start * np.cumprod(1.0 + rets)


def _uptrend(rng: np.random.Generator, n: int, start: float) -> np.ndarray:
    drift = rng.uniform(0.0004, 0.0011)
    noise = rng.normal(0, 0.0009, n)
    return _walk(start, drift + noise)


def _downtrend(rng: np.random.Generator, n: int, start: float) -> np.ndarray:
    drift = rng.uniform(0.0004, 0.0011)
    noise = rng.normal(0, 0.0009, n)
    return _walk(start, -drift + noise)


def _choppy_range(rng: np.random.Generator, n: int, start: float) -> np.ndarray:
    """Mean-reverting range -- the whipsaw that eats trend-followers alive."""
    half_life = rng.uniform(8, 20)
    theta = 1.0 - 0.5 ** (1.0 / half_life)
    vol = rng.uniform(0.0007, 0.0016)
    log_mid = np.log(start)
    x = log_mid
    out = np.empty(n)
    for i in range(n):
        x += theta * (log_mid - x) + rng.normal(0, vol)
        out[i] = np.exp(x)
    return out


def _flash_crash(rng: np.random.Generator, n: int, start: float) -> np.ndarray:
    """Calm, a violent single-direction plunge, then a partial bounce."""
    rets = rng.normal(0, 0.0006, n)
    crash_at = rng.integers(n // 3, 2 * n // 3)
    depth = rng.uniform(0.03, 0.07)
    rets[crash_at] = -depth
    # partial recovery over the next few bars
    for k in range(1, rng.integers(4, 10)):
        if crash_at + k < n:
            rets[crash_at + k] += depth * rng.uniform(0.05, 0.15)
    return _walk(start, rets)


def _news_spike(rng: np.random.Generator, n: int, start: float) -> np.ndarray:
    """Flat, then a two-sided violent spike (the release), then it settles --
    the exact bar the desk is designed to DODGE, not chase."""
    rets = rng.normal(0, 0.0004, n)
    spike_at = rng.integers(n // 3, 2 * n // 3)
    direction = rng.choice([-1.0, 1.0])
    mag = rng.uniform(0.015, 0.04)
    rets[spike_at] = direction * mag
    if spike_at + 1 < n:  # instant partial mean-reversion (the whipsaw)
        rets[spike_at + 1] = -direction * mag * rng.uniform(0.4, 0.8)
    return _walk(start, rets)


def _low_vol_grind(rng: np.random.Generator, n: int, start: float) -> np.ndarray:
    drift = rng.uniform(-0.00015, 0.00015)
    noise = rng.normal(0, 0.0003, n)
    return _walk(start, drift + noise)


def _high_vol_whipsaw(rng: np.random.Generator, n: int, start: float) -> np.ndarray:
    period = rng.uniform(15, 40)
    amp = rng.uniform(0.004, 0.009)
    t = np.arange(n)
    swing = amp * np.sin(2 * np.pi * t / period)
    noise = rng.normal(0, 0.0015, n)
    return start * (1.0 + swing) * np.cumprod(1.0 + noise)


def _gap_up(rng: np.random.Generator, n: int, start: float) -> np.ndarray:
    gap = rng.uniform(0.01, 0.03)
    rets = rng.normal(0.0001, 0.0008, n)
    rets[0] = gap  # the opening gap
    return _walk(start, rets)


def _gap_down(rng: np.random.Generator, n: int, start: float) -> np.ndarray:
    gap = rng.uniform(0.01, 0.03)
    rets = rng.normal(-0.0001, 0.0008, n)
    rets[0] = -gap
    return _walk(start, rets)


def _v_reversal(rng: np.random.Generator, n: int, start: float) -> np.ndarray:
    """Sell-off into a hard reversal (or the mirror image)."""
    turn = rng.integers(n // 3, 2 * n // 3)
    leg = rng.uniform(0.0006, 0.0013)
    up_first = rng.random() < 0.5
    rets = np.empty(n)
    s1 = leg if up_first else -leg
    rets[:turn] = s1 + rng.normal(0, 0.0008, turn)
    rets[turn:] = -s1 + rng.normal(0, 0.0008, n - turn)
    return _walk(start, rets)


def _blowoff_top(rng: np.random.Generator, n: int, start: float) -> np.ndarray:
    """Accelerating parabola, then a sharp give-back -- the classic trap."""
    peak = rng.integers(2 * n // 3, max(2 * n // 3 + 1, n - 5))
    accel = np.linspace(0.0002, rng.uniform(0.0015, 0.003), peak)
    rets = np.empty(n)
    rets[:peak] = accel + rng.normal(0, 0.0006, peak)
    rets[peak:] = rng.uniform(-0.006, -0.002) + rng.normal(0, 0.0009, n - peak)
    return _walk(start, rets)


def _breakout(rng: np.random.Generator, n: int, start: float) -> np.ndarray:
    """A tight range that resolves into a clean breakout with follow-through."""
    brk = rng.integers(n // 3, 2 * n // 3)
    rets = np.empty(n)
    rets[:brk] = rng.normal(0, 0.0004, brk)  # coil
    direction = rng.choice([-1.0, 1.0])
    rets[brk] = direction * rng.uniform(0.006, 0.012)  # the break bar
    rets[brk + 1:] = direction * rng.uniform(0.0004, 0.0009) + rng.normal(0, 0.0007, n - brk - 1)
    return _walk(start, rets)


def _trend_pullback(rng: np.random.Generator, n: int, start: float) -> np.ndarray:
    """A real trend: mostly one way, with periodic counter-trend pullbacks."""
    drift = rng.uniform(0.0004, 0.0009) * rng.choice([-1.0, 1.0])
    rets = drift + rng.normal(0, 0.0008, n)
    step = int(rng.integers(18, 35))
    for pb in range(step, n, step):  # a pullback every ~step bars
        span = min(rng.integers(3, 7), n - pb)
        rets[pb:pb + span] = -drift * rng.uniform(1.5, 3.0)
    return _walk(start, rets)


# Ordered so `generate_scenarios` cycles a balanced mix of good and bad tape.
REGIMES: Dict[str, Callable[[np.random.Generator, int, float], np.ndarray]] = {
    "uptrend": _uptrend,
    "downtrend": _downtrend,
    "choppy_range": _choppy_range,
    "flash_crash": _flash_crash,
    "news_spike": _news_spike,
    "low_vol_grind": _low_vol_grind,
    "high_vol_whipsaw": _high_vol_whipsaw,
    "gap_up": _gap_up,
    "gap_down": _gap_down,
    "v_reversal": _v_reversal,
    "blowoff_top": _blowoff_top,
    "breakout": _breakout,
    "trend_pullback": _trend_pullback,
}


def _ohlc_from_close(
    close: np.ndarray, rng: np.random.Generator, bars: int
) -> pd.DataFrame:
    """Wrap a close path in a plausible 1-minute OHLCV frame.

    A 1-minute DatetimeIndex is deliberate: agent.py detects intraday spacing
    and switches on the VWAP / opening-range / session-phase features, so the
    agent practises with the *same* state representation it uses live.
    """
    close = np.asarray(close, dtype=float)
    open_ = np.empty_like(close)
    open_[0] = close[0]
    open_[1:] = close[:-1]
    body_hi = np.maximum(open_, close)
    body_lo = np.minimum(open_, close)
    wick = np.abs(close - open_) + close * rng.uniform(0.0002, 0.0006, len(close))
    high = body_hi + wick * rng.uniform(0.1, 0.6, len(close))
    low = body_lo - wick * rng.uniform(0.1, 0.6, len(close))
    # volume rises with bar range -- big bars trade heavier, like real tape
    rng_pct = (high - low) / np.maximum(close, 1e-9)
    volume = 1000.0 * (1.0 + 40.0 * rng_pct) * rng.uniform(0.8, 1.2, len(close))
    idx = pd.date_range("2025-01-02 09:30", periods=bars, freq="1min")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def generate_scenarios(
    n: int = 300, bars: int = 200, seed: int = 0
) -> List[Tuple[str, pd.DataFrame]]:
    """Produce `n` labelled synthetic sessions, cycling the regime catalog so
    good tape (trends, breakouts) and bad tape (chop, crashes, news spikes)
    are represented in equal measure. Deterministic for a given `seed`."""
    names = list(REGIMES)
    scenarios: List[Tuple[str, pd.DataFrame]] = []
    for i in range(n):
        name = names[i % len(names)]
        rng = np.random.default_rng(seed * 100_000 + i)
        start = float(rng.uniform(50, 200))
        close = REGIMES[name](rng, bars, start)
        scenarios.append((name, _ohlc_from_close(close, rng, bars)))
    return scenarios


def run_practice(
    n_scenarios: int = 300,
    bars: int = 200,
    episodes_per: int = 2,
    agent: Optional[QTraderAgent] = None,
    seed: int = 0,
) -> Dict[str, object]:
    """Train/evaluate the agent across `n_scenarios` synthetic regimes.

    If `agent` is given, its Q-table is *hardened in place* (continued
    learning); otherwise a throwaway agent practises so nothing live is
    touched. Returns overall stats plus a per-regime breakdown -- the
    per-regime numbers are the point: they show which tape the policy handles
    and which it bleeds on.

    Never writes to the journal or the paper account. Never saves the Q-table
    (the caller decides that, so synthetic training can't silently overwrite
    the live policy).
    """
    agent = agent or QTraderAgent(model_path="/dev/null")
    warmup = 30
    per_regime: Dict[str, Dict[str, float]] = {}

    for name, df in generate_scenarios(n_scenarios, bars=bars, seed=seed):
        if len(df) < warmup * 4:
            continue
        # practice: explore + learn on this regime
        for _ in range(episodes_per):
            agent._run_episode(df, warmup=warmup, explore=True)
            agent.trained_episodes += 1
        # measure: one greedy pass, no learning
        result = agent._run_episode(df, warmup=warmup, explore=False)
        acc = per_regime.setdefault(
            name, {"scenarios": 0, "trades": 0, "wins": 0.0, "return_pct": 0.0}
        )
        acc["scenarios"] += 1
        acc["trades"] += result["trades"]
        acc["wins"] += result["win_rate"] * result["trades"]
        acc["return_pct"] += result["total_return_pct"]

    regimes_report: Dict[str, Dict[str, float]] = {}
    total_return = 0.0
    total_trades = 0
    total_wins = 0.0
    for name, acc in sorted(per_regime.items()):
        trades = int(acc["trades"])
        regimes_report[name] = {
            "scenarios": int(acc["scenarios"]),
            "trades": trades,
            "win_rate": (acc["wins"] / trades) if trades else 0.0,
            "avg_return_pct": acc["return_pct"] / acc["scenarios"] if acc["scenarios"] else 0.0,
        }
        total_return += acc["return_pct"]
        total_trades += trades
        total_wins += acc["wins"]

    scenarios_run = sum(int(a["scenarios"]) for a in per_regime.values())
    return {
        "scenarios": scenarios_run,
        "total_trades": total_trades,
        "overall_win_rate": (total_wins / total_trades) if total_trades else 0.0,
        "avg_return_pct": (total_return / scenarios_run) if scenarios_run else 0.0,
        "worst_regime": min(
            regimes_report,
            key=lambda k: regimes_report[k]["avg_return_pct"],
        ) if regimes_report else None,
        "best_regime": max(
            regimes_report,
            key=lambda k: regimes_report[k]["avg_return_pct"],
        ) if regimes_report else None,
        "by_regime": regimes_report,
        "trained_episodes": agent.trained_episodes,
    }
