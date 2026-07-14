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
from datetime import date
from typing import Tuple

DEFAULT_STATE_PATH = os.path.join("bot_data", "day_state.json")


class DrawdownGuard:
    def __init__(
        self,
        max_daily_loss_pct: float = 0.05,
        state_path: str = DEFAULT_STATE_PATH,
    ):
        self.max_daily_loss_pct = max_daily_loss_pct
        self.state_path = state_path

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
        """Returns (halted, message). Call once per desk cycle."""
        today = date.today().isoformat()
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
