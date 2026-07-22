"""Monte Carlo estimate of the odds of PASSING a specific prop-firm
challenge, instead of quoting an industry-average pass rate.

The industry number (~5-14% of all challenge attempts succeed, mostly
because traders over-risk and blow the drawdown limit -- see session 46
docs) describes an average human, not this bot's actual risk behavior.
This module runs the trained Q-agent bar-by-bar through many synthetic
multi-regime "market histories" under the EXACT challenge rules (daily
loss halt, max-drawdown fail, profit-target pass) and measures the real
fraction of simulated attempts that pass before failing.

Honest scope: this uses the core Q-agent + risk-per-trade sizing + the
challenge's own daily/max/target thresholds. It does NOT run the full
desk's entry filters (ADX regime, zone touches, ORB retest, ATR stops,
news blackout, spread veto, etc.) -- those only remove marginal/bad
trades and raise trade quality, so leaving them out is a conservative
(pessimistic), not optimistic, simplification. This is still a synthetic
estimate, not a guarantee -- see the honesty norms in CLAUDE.md.

    python -m bots challenge-odds --attempts 300
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import numpy as np
import pandas as pd

from bots.learning.agent import QTraderAgent, extract_state
from bots.learning.scenarios import REGIMES, _ohlc_from_close


def _generate_attempt_tape(rng: np.random.Generator, bars: int) -> pd.DataFrame:
    """One continuous synthetic price history for a single simulated
    attempt: several regimes chained back to back (real markets aren't one
    pure regime for weeks), same 1-minute OHLCV shape the live desk uses."""
    names = list(REGIMES)
    rng.shuffle(names)
    leg_bars = max(60, bars // len(names))
    closes: List[np.ndarray] = []
    price = float(rng.uniform(50, 200))
    for name in names:
        leg = REGIMES[name](rng, leg_bars, price)
        closes.append(leg)
        price = float(leg[-1])
    close = np.concatenate(closes)[:bars] if sum(len(c) for c in closes) >= bars else np.concatenate(closes)
    return _ohlc_from_close(close, rng, len(close))


@dataclass
class AttemptResult:
    outcome: str  # "pass" | "fail" | "undecided"
    bars_used: int
    final_gain_pct: float


def simulate_attempt(
    agent: QTraderAgent,
    df: pd.DataFrame,
    start_equity: float = 5_000.0,
    risk_per_trade_pct: float = 0.015,
    stop_loss_pct: float = 0.015,
    target_pct: float = 0.10,
    daily_loss_pct: float = 0.04,
    max_drawdown_pct: float = 0.06,
    warmup: int = 30,
    atr_stops: bool = False,
    atr_window: int = 14,
) -> AttemptResult:
    """Walk one synthetic price history bar-by-bar under the challenge's
    actual rules. Position P&L is scaled by risk_per_trade_pct/stop_loss_pct
    so a full stop-out costs exactly risk_per_trade_pct of equity, matching
    how the real desk sizes positions (see risk.py / organization.py).

    atr_stops (session 47): the funded live desk runs with atr_stops=True --
    stop distance is 1.5x ATR(14), clamped [0.3%, 5%], not a fixed
    stop_loss_pct. Previously this simulation always used the fixed value,
    a disclosed simplification; when atr_stops is set, each entry's
    risk_scale is computed from the REAL rolling ATR of the bootstrapped
    real price data at entry time, same clamp as organization.py."""
    risk_scale = risk_per_trade_pct / stop_loss_pct if stop_loss_pct > 0 else 1.0
    equity = start_equity
    peak_equity = start_equity
    day_start_equity = start_equity
    day_halted = False
    current_day = None
    holding = False
    entry_price = 0.0
    trade_risk_scale = risk_scale

    idx = df.index
    for i in range(warmup, len(df) - 1):
        day = idx[i].normalize() if isinstance(idx, pd.DatetimeIndex) else None
        if day is not None and day != current_day:
            current_day = day
            day_start_equity = equity
            day_halted = False
        if not day_halted and day_start_equity > 0:
            day_gain = (equity - day_start_equity) / day_start_equity
            if day_gain <= -daily_loss_pct:
                day_halted = True

        peak_equity = max(peak_equity, equity)
        if peak_equity > 0 and (equity - peak_equity) / peak_equity <= -max_drawdown_pct:
            return AttemptResult("fail", i - warmup, (equity - start_equity) / start_equity)

        gain = (equity - start_equity) / start_equity
        if gain >= target_pct:
            return AttemptResult("pass", i - warmup, gain)

        price = float(df["close"].iloc[i])
        next_price = float(df["close"].iloc[i + 1])
        state = extract_state(df, i, holding)
        action = "hold" if day_halted and not holding else agent.choose_action(state, explore=False)

        if action == "buy" and not holding:
            holding = True
            entry_price = price
            if atr_stops:
                from bots.organization import atr_pct

                entry_stop_pct = atr_pct(df.iloc[: i + 1].tail(atr_window + 1), period=atr_window)
                entry_stop_pct = min(max(1.5 * entry_stop_pct, 0.003), 0.05) if entry_stop_pct else stop_loss_pct
                trade_risk_scale = risk_per_trade_pct / entry_stop_pct
            else:
                trade_risk_scale = risk_scale
        elif action == "sell" and holding:
            trade_return = (price - entry_price) / entry_price
            equity *= 1.0 + trade_return * trade_risk_scale
            holding = False
        elif holding:
            # mark-to-market on the open position each bar
            step_return = (next_price - price) / price
            equity *= 1.0 + step_return * trade_risk_scale

    return AttemptResult("undecided", len(df) - warmup, (equity - start_equity) / start_equity)


def run_challenge_monte_carlo(
    n_attempts: int = 300,
    bars_per_attempt: int = 3000,
    agent: "QTraderAgent | None" = None,
    seed: int = 0,
    **rule_kwargs,
) -> Dict[str, object]:
    """Run `n_attempts` independent simulated challenge attempts and report
    the empirical pass/fail/undecided rates. Loads the live-trained Q-table
    if `agent` isn't supplied (so the estimate reflects the actual policy
    that would run on the real account, not a fresh/untrained one)."""
    if agent is None:
        agent = QTraderAgent()
        agent.load()  # live qtable.json -- reflects the policy that would actually run

    outcomes = {"pass": 0, "fail": 0, "undecided": 0}
    gains: List[float] = []
    bars_to_pass: List[int] = []
    for i in range(n_attempts):
        rng = np.random.default_rng(seed * 1_000_003 + i)
        df = _generate_attempt_tape(rng, bars_per_attempt)
        result = simulate_attempt(agent, df, **rule_kwargs)
        outcomes[result.outcome] += 1
        gains.append(result.final_gain_pct)
        if result.outcome == "pass":
            bars_to_pass.append(result.bars_used)

    n = max(n_attempts, 1)
    return {
        "attempts": n_attempts,
        "pass_rate": outcomes["pass"] / n,
        "fail_rate": outcomes["fail"] / n,
        "undecided_rate": outcomes["undecided"] / n,
        "avg_final_gain_pct": float(np.mean(gains)) * 100.0 if gains else 0.0,
        "avg_bars_to_pass": float(np.mean(bars_to_pass)) if bars_to_pass else None,
    }
