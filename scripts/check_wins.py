"""Check for newly-closed winning trades since the last check.

Used by the "text me when it makes money" notification loop: prints one
line per win not seen before, and remembers what it's already reported in
a small marker file so re-running doesn't repeat the same trade.
"""

from __future__ import annotations

import json
import os

from bots.journal import TradeJournal
from bots.paths import data_path

MARKER_PATH = data_path("last_win_notified.json")


def _load_seen() -> set:
    if not os.path.exists(MARKER_PATH):
        return set()
    with open(MARKER_PATH, "r", encoding="utf-8") as fh:
        return set(json.load(fh).get("seen", []))


def _save_seen(seen: set) -> None:
    os.makedirs(os.path.dirname(MARKER_PATH) or ".", exist_ok=True)
    with open(MARKER_PATH, "w", encoding="utf-8") as fh:
        json.dump({"seen": sorted(seen)}, fh, indent=2)


def new_wins() -> list:
    journal = TradeJournal()
    seen = _load_seen()
    wins = [
        t for t in journal.trades.values()
        if not t.is_open and t.pnl is not None and t.pnl > 0
        and "admin" not in t.tags and t.trade_id not in seen
    ]
    wins.sort(key=lambda t: t.exit_time or "")
    _save_seen(seen | {t.trade_id for t in wins})
    return wins


if __name__ == "__main__":
    for t in new_wins():
        print(f"{t.symbol} {t.side} +{t.pnl:.2f} ({t.pnl_pct:+.2f}%) setup={t.setup}")
