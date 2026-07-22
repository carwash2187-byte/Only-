"""Account-level loss protection.

DrawdownGuard enforces the "never lose more than X% in a day" rule that
disciplined scalpers live by: it records the account's equity at the first
check of each day, and once equity has fallen more than `max_daily_loss_pct`
below that mark, the trading desk stops opening new positions until the next
day. Code-enforced, no willpower required.
"""

from __future__ import annotations

import json
import os
from typing import Optional, Tuple

from bots.paths import data_path


class DrawdownGuard:
    def __init__(
        self,
        max_daily_loss_pct: float = 0.05,
        state_path: Optional[str] = None,
    ):
        self.max_daily_loss_pct = max_daily_loss_pct
        self.state_path = state_path or data_path("day_state.json")

    def _load(self) -> dict:
        if not os.path.exists(self.state_path):
            return {}
        with open(self.state_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, state: dict) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)

    def check(self, equity: float) -> Tuple[bool, str]:
        """Returns (halted, message). Call once per desk cycle. The 'day'
        rolls at 5pm New York time (the forex/funded-account daily close),
        not container-local midnight -- same convention as the journal's
        per-day counters (see bots.journal.trading_day)."""
        from bots.journal import trading_day

        today = trading_day()
        state = self._load()
        if state.get("date") != today:
            state = {"date": today, "start_equity": equity}
            self._save(state)
            return False, f"day start equity recorded: {equity:.2f}"

        start = float(state.get("start_equity") or equity)
        if start <= 0:
            return False, "no baseline equity"
        loss_pct = 1.0 - equity / start
        if loss_pct >= self.max_daily_loss_pct:
            return True, (
                f"CIRCUIT BREAKER: down {loss_pct:.1%} today "
                f"(from {start:.2f} to {equity:.2f}), max is "
                f"{self.max_daily_loss_pct:.0%} -- no new trades until tomorrow"
            )
        return False, f"daily drawdown {loss_pct:+.1%} (limit {self.max_daily_loss_pct:.0%})"


    def day_gain_pct(self, equity: float) -> float:
        """Today's gain vs the recorded day-start baseline (0.0 if no
        baseline yet). Used by the daily profit target ('quit while
        ahead') rule."""
        from bots.journal import trading_day

        state = self._load()
        if state.get("date") != trading_day():
            return 0.0
        start = float(state.get("start_equity") or 0.0)
        return (equity / start - 1.0) if start > 0 else 0.0

    def loss_headroom_pct(self, equity: float, budget_pct: float = 0.8) -> float:
        """How much more (as a fraction of CURRENT equity) the account can
        lose before consuming `budget_pct` of the daily loss limit.

        The unconsumed (1 - budget_pct) is deliberately never risked: stops
        are not guaranteed fills, and slippage/spread widening on a fast
        move routinely pushes the realized loss past the intended stop. On
        a funded account the daily limit is a hard termination line, so the
        desk budgets only 80% of it by default and keeps 20% as slippage
        insurance -- the standard buffer discipline funded traders use.
        """
        from bots.journal import trading_day

        usable = self.max_daily_loss_pct * budget_pct
        state = self._load()
        if state.get("date") != trading_day():
            return usable  # fresh day, full budget
        start = float(state.get("start_equity") or 0.0)
        if start <= 0 or equity <= 0:
            return usable
        floor_equity = start * (1.0 - usable)
        return max((equity - floor_equity) / equity, 0.0)


class MaxDrawdownGuard:
    """The other funded-account limit: total drawdown from the account's
    all-time peak equity (not reset daily like DrawdownGuard). Breaching
    this is typically permanent account termination at real prop firms --
    the guard halts ALL new entries once breached, not just for the day,
    until manually cleared (reset the state file after reviewing what
    happened)."""

    def __init__(self, max_total_drawdown_pct: float = 0.05, state_path: Optional[str] = None):
        self.max_total_drawdown_pct = max_total_drawdown_pct
        self.state_path = state_path or data_path("max_drawdown_state.json")

    def _load(self) -> dict:
        if not os.path.exists(self.state_path):
            return {}
        with open(self.state_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, state: dict) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)

    def check(self, equity: float) -> Tuple[bool, str]:
        state = self._load()
        if state.get("breached"):
            return True, (
                f"ACCOUNT HALTED: max drawdown of {self.max_total_drawdown_pct:.0%} was "
                f"breached at peak {state['peak_equity']:.2f} -- clear "
                f"{os.path.basename(self.state_path)} after reviewing what happened"
            )
        peak = max(float(state.get("peak_equity", equity)), equity)
        drawdown = 1.0 - equity / peak if peak > 0 else 0.0
        breached = drawdown >= self.max_total_drawdown_pct
        self._save({"peak_equity": peak, "breached": breached})
        if breached:
            return True, (
                f"ACCOUNT HALTED: total drawdown {drawdown:.1%} from peak {peak:.2f} "
                f"breached the {self.max_total_drawdown_pct:.0%} max -- this is how "
                f"funded accounts get terminated, no new trades until manually reviewed"
            )
        return False, f"total drawdown from peak: {drawdown:.1%} (limit {self.max_total_drawdown_pct:.0%})"

    def loss_headroom_pct(self, equity: float, budget_pct: float = 0.8) -> float:
        """How much more (as a fraction of CURRENT equity) the account can
        lose before consuming `budget_pct` of the max total drawdown from
        peak. Same slippage-insurance logic as the daily version: the last
        (1 - budget_pct) of the ceiling is never knowingly put at risk."""
        state = self._load()
        peak = float(state.get("peak_equity") or equity)
        if peak <= 0 or equity <= 0:
            return self.max_total_drawdown_pct * budget_pct
        floor_equity = peak * (1.0 - self.max_total_drawdown_pct * budget_pct)
        return max((equity - floor_equity) / equity, 0.0)

    def size_multiplier(self, equity: float) -> float:
        """Taper position size down as drawdown approaches the hard ceiling,
        instead of trading at full size right up until check() slams the
        door shut. Standard prop-desk practice (tiered drawdown limits):
        below half the max, full size; 50-75% of the way there, half size;
        75-100%, quarter size. 1.0 if there's no baseline yet."""
        state = self._load()
        peak = float(state.get("peak_equity") or equity)
        if peak <= 0 or self.max_total_drawdown_pct <= 0:
            return 1.0
        drawdown = 1.0 - equity / peak
        used = drawdown / self.max_total_drawdown_pct  # fraction of the ceiling used up
        if used < 0.5:
            return 1.0
        if used < 0.75:
            return 0.5
        return 0.25


class ChallengeTargetGuard:
    """Lock in a prop-firm evaluation pass instead of trading past it.

    A challenge has a one-way finish line: hit the cumulative profit target
    and you pass, but nothing stops the desk from giving it back on the next
    bad trade if it just keeps going. This tracks the challenge's starting
    equity permanently (unlike DrawdownGuard's daily reset), and once
    cumulative gain reaches `target_pct`, halts ALL new entries for good --
    existing positions still get managed/exited normally, but no new risk
    is taken once the pass is banked. Once tripped, stays tripped until the
    state file is cleared (matches MaxDrawdownGuard's permanent-halt model)."""

    def __init__(self, target_pct: float = 0.0, state_path: Optional[str] = None):
        self.target_pct = target_pct
        self.state_path = state_path or data_path("challenge_target_state.json")

    def _load(self) -> dict:
        if not os.path.exists(self.state_path):
            return {}
        with open(self.state_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, state: dict) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)

    def check(self, equity: float) -> Tuple[bool, str]:
        if self.target_pct <= 0:
            return False, "challenge target lock: off"
        state = self._load()
        start = float(state.get("start_equity") or equity)
        if "start_equity" not in state:
            self._save({"start_equity": start, "target_hit": False})
        if state.get("target_hit"):
            return True, (
                f"CHALLENGE TARGET LOCKED: +{self.target_pct:.0%} was reached from "
                f"starting equity {start:.2f} -- no new entries, the pass is banked"
            )
        gain = (equity - start) / start if start > 0 else 0.0
        hit = gain >= self.target_pct
        if hit:
            self._save({"start_equity": start, "target_hit": True})
            return True, (
                f"CHALLENGE TARGET HIT: {gain:+.1%} from start -- locking in the pass, "
                "no new entries (clear the state file once funded/reset for a new challenge)"
            )
        return False, f"challenge progress: {gain:+.1%} (target {self.target_pct:.0%})"
