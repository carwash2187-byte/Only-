"""Session 48: Instant-Funded simulation + risk-level sweep (token-free).

The challenge sim answers "can it hit +10% before -6%?". An INSTANT funded
account (AquaFunded/Clarity Instant style) has no profit target: the only
ways to lose are the 3% daily halt (temporary) and the 6% max drawdown
(account gone). What matters is (a) probability of surviving a ~30-real-
trading-day month, and (b) how much profit a surviving month produces --
that's what becomes payouts.

This sweeps risk_per_trade_pct across candidate levels on the same
block-bootstrapped REAL tapes (identical seeds per level, so levels are
compared on the SAME markets), with ATR stops on, using the live Q-table.
Pure python -- no Claude usage.

    BOT_DATA_DIR=paper_state PYTHONPATH=. python scripts/run_instant_odds.py
"""
from __future__ import annotations

import json
import time

from bots.learning.agent import QTraderAgent
from bots.learning.challenge_sim import simulate_attempt
from bots.learning.challenge_sim_real import _block_bootstrap_tape, fetch_real_pools

import numpy as np

RISK_LEVELS = [0.005, 0.0075, 0.010, 0.0125, 0.015]
N_SEEDS = 3
ATTEMPTS_PER_SEED = 40
BARS = 45_000  # ~30 real trading days of 1m bars
OUT_PATH = "/tmp/instant_odds_result.json"

# Instant-account rules (AquaFunded Instant / Clarity Instant shape):
# 3% daily halt, 6% max drawdown, NO pass target -- so target_pct is set
# unreachably high and "undecided" means SURVIVED the month.
INSTANT_RULES = dict(target_pct=99.0, daily_loss_pct=0.03,
                     max_drawdown_pct=0.06, atr_stops=True)


def main() -> None:
    t0 = time.time()
    agent = QTraderAgent()
    loaded = agent.load()
    print(f"live model: {'loaded' if loaded else 'FRESH'} ({len(agent.q)} states)")
    pools = fetch_real_pools()
    print(f"got {len(pools)} symbols: {sorted(pools)}")
    if not pools:
        raise SystemExit("no real market data -- aborting")

    results = {}
    for risk in RISK_LEVELS:
        survived = busted = 0
        gains = []
        surviving_gains = []
        for seed in range(N_SEEDS):
            for i in range(ATTEMPTS_PER_SEED):
                rng = np.random.default_rng(seed * 7_919 + i)  # same tapes per level
                df = _block_bootstrap_tape(pools, rng, BARS)
                r = simulate_attempt(agent, df, risk_per_trade_pct=risk,
                                     **INSTANT_RULES)
                gains.append(r.final_gain_pct)
                if r.outcome == "fail":
                    busted += 1
                else:
                    survived += 1
                    surviving_gains.append(r.final_gain_pct)
        n = survived + busted
        results[f"{risk:.4f}"] = {
            "risk_per_trade_pct": risk,
            "attempts": n,
            "survival_rate": survived / n,
            "bust_rate": busted / n,
            "avg_gain_all_pct": float(np.mean(gains)) * 100,
            "median_gain_all_pct": float(np.median(gains)) * 100,
            "avg_gain_surviving_pct": (float(np.mean(surviving_gains)) * 100
                                       if surviving_gains else 0.0),
        }
        r_ = results[f"{risk:.4f}"]
        print(f"[risk {risk:.2%}] survive {r_['survival_rate']:.1%}  "
              f"bust {r_['bust_rate']:.1%}  avg month {r_['avg_gain_all_pct']:+.2f}%  "
              f"(survivors {r_['avg_gain_surviving_pct']:+.2f}%)  "
              f"elapsed {(time.time()-t0)/60:.0f}m")

    out = {
        "rules": {k: v for k, v in INSTANT_RULES.items()},
        "n_seeds": N_SEEDS, "attempts_per_seed": ATTEMPTS_PER_SEED,
        "bars_per_attempt": BARS, "symbols_used": sorted(pools),
        "per_risk_level": results,
        "elapsed_minutes": (time.time() - t0) / 60.0,
    }
    with open(OUT_PATH, "w") as fh:
        json.dump(out, fh, indent=2)
    print(f"\nDONE -- wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
