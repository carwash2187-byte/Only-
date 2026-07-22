"""Session 47: large multi-seed real-data challenge pass-rate run.

Fetches the live desk's real 19-symbol watchlist ONCE, then runs several
independent seeds of the real-data block-bootstrap Monte Carlo
(bots.learning.challenge_sim_real) with ATR-based per-trade risk sizing
(matching the live funded desk's atr_stops=True) against the CURRENT
live-trained Q-table. Pure python/pandas/numpy -- no Claude usage.

    BOT_DATA_DIR=paper_state PYTHONPATH=. python scripts/run_challenge_odds.py
"""
from __future__ import annotations

import json
import time

import numpy as np

from bots.learning.agent import QTraderAgent
from bots.learning.challenge_sim_real import fetch_real_pools, run_real_data_monte_carlo

N_SEEDS = 5
ATTEMPTS_PER_SEED = 100
BARS_PER_ATTEMPT = 45_000  # ~30 real trading days, matches session 46's horizon
OUT_PATH = "/tmp/challenge_odds_result.json"

RULES = dict(
    target_pct=0.10,       # Clarity One-Step challenge phase: 10% target
    daily_loss_pct=0.04,   # 4% daily loss limit
    max_drawdown_pct=0.06, # 6% max total drawdown
    risk_per_trade_pct=0.015,
    atr_stops=True,        # session 47: real ATR sizing, not a fixed stop pct
)


def main() -> None:
    t0 = time.time()
    agent = QTraderAgent()
    loaded = agent.load()
    print(f"live model: {'loaded' if loaded else 'FRESH, no qtable found'} "
          f"({len(agent.q)} known states)")

    print("fetching real market data for the full live watchlist...")
    pools = fetch_real_pools()
    print(f"got {len(pools)}/{19} symbols: {sorted(pools)}")
    if not pools:
        raise SystemExit("no real market data available -- aborting")

    all_results = []
    total_pass = total_fail = total_undecided = 0
    total_attempts = 0
    for seed in range(N_SEEDS):
        r = run_real_data_monte_carlo(
            n_attempts=ATTEMPTS_PER_SEED,
            bars_per_attempt=BARS_PER_ATTEMPT,
            agent=agent,
            pools=pools,
            seed=seed,
            **RULES,
        )
        elapsed = time.time() - t0
        print(f"[seed {seed}] {ATTEMPTS_PER_SEED} attempts: "
              f"pass {r['pass_rate']:.1%}  fail {r['fail_rate']:.1%}  "
              f"undecided {r['undecided_rate']:.1%}  "
              f"avg gain {r['avg_final_gain_pct']:+.2f}%  "
              f"(elapsed {elapsed/60:.1f} min)")
        all_results.append(r)
        total_pass += round(r["pass_rate"] * ATTEMPTS_PER_SEED)
        total_fail += round(r["fail_rate"] * ATTEMPTS_PER_SEED)
        total_undecided += round(r["undecided_rate"] * ATTEMPTS_PER_SEED)
        total_attempts += ATTEMPTS_PER_SEED

    combined = {
        "total_attempts": total_attempts,
        "n_seeds": N_SEEDS,
        "attempts_per_seed": ATTEMPTS_PER_SEED,
        "bars_per_attempt": BARS_PER_ATTEMPT,
        "rules": RULES,
        "symbols_used": sorted(pools),
        "pass_rate": total_pass / total_attempts,
        "fail_rate": total_fail / total_attempts,
        "undecided_rate": total_undecided / total_attempts,
        "per_seed": all_results,
        "elapsed_minutes": (time.time() - t0) / 60.0,
    }
    with open(OUT_PATH, "w") as fh:
        json.dump(combined, fh, indent=2)

    print(f"\n=== COMBINED across {N_SEEDS} seeds, {total_attempts} total attempts ===")
    print(f"  PASS:      {combined['pass_rate']:.1%}")
    print(f"  FAIL:      {combined['fail_rate']:.1%}")
    print(f"  undecided: {combined['undecided_rate']:.1%}")
    print(f"  wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
