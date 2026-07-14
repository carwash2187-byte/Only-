"""Mirror trades called out by a human you follow (Instagram, YouTube,
Discord, Telegram...).

There is no API for an influencer's Instagram stories, and their win-rate
claims can't be verified -- so this works the honest way around: YOU record
the call, the desk executes it under ITS risk rules (per-trade risk cap,
stop-loss, daily 5% circuit breaker), and the trade journal tracks each
source's real performance in your account. If a source's calls lose money
over enough trades, the journal's lesson system auto-blocks copying them.

    python -m bots mirror EURUSD --side buy --source mambafx --note "IG story 7/14"
    python -m bots trade            # desk picks the call up, applies risk rules
    python -m bots journal          # shows whether mirror:mambafx actually earns
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import List

DEFAULT_SIGNALS_PATH = os.path.join("bot_data", "manual_signals.json")


@dataclass
class ManualSignal:
    symbol: str
    side: str  # "buy" or "sell"
    source: str  # who called it, e.g. "mambafx"
    added: str
    note: str = ""

    @property
    def setup(self) -> str:
        return f"mirror:{self.source}"


def _load(path: str) -> List[ManualSignal]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [ManualSignal(**item) for item in json.load(fh)]


def _save(signals: List[ManualSignal], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([asdict(s) for s in signals], fh, indent=2)


def add_signal(
    symbol: str,
    side: str = "buy",
    source: str = "manual",
    note: str = "",
    path: str = DEFAULT_SIGNALS_PATH,
) -> ManualSignal:
    signal = ManualSignal(
        symbol=symbol.upper(),
        side=side.lower(),
        source=source.lower(),
        added=datetime.now(timezone.utc).isoformat(),
        note=note,
    )
    signals = [s for s in _load(path) if s.symbol != signal.symbol]
    signals.append(signal)
    _save(signals, path)
    return signal


def pending_signals(path: str = DEFAULT_SIGNALS_PATH) -> List[ManualSignal]:
    return _load(path)


def consume_signal(symbol: str, path: str = DEFAULT_SIGNALS_PATH) -> None:
    _save([s for s in _load(path) if s.symbol != symbol.upper()], path)
