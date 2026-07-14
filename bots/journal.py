"""Trade journal: the learn-from-mistakes memory shared by every bot.

Every trade is recorded with the "setup" that produced it (e.g. which signal
source and market regime). The journal aggregates outcomes per setup so the
trading desk can veto setups that have historically lost money -- the
organization-level version of learning from mistakes, mirroring the reflection
memory TradingAgents uses for its LLM agents.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bots.paths import data_path

# A setup needs at least this many closed trades before its stats are trusted
# enough to veto new entries.
MIN_TRADES_FOR_LESSON = 5


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    side: str  # "long" or "short"
    quantity: float
    entry_price: float
    entry_time: str
    setup: str  # e.g. "copytrade:13f-consensus", "rl:trend-up-rsi-low"
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    notes: str = ""
    tags: List[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.exit_price is None


class TradeJournal:
    """Append-only journal persisted to JSON."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or data_path("trade_journal.json")
        self.trades: Dict[str, TradeRecord] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        for item in raw.get("trades", []):
            record = TradeRecord(**item)
            self.trades[record.trade_id] = record

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        payload = {"trades": [asdict(t) for t in self.trades.values()]}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self.path)

    # -- recording ----------------------------------------------------------

    def open_trade(
        self,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        setup: str,
        notes: str = "",
    ) -> TradeRecord:
        record = TradeRecord(
            trade_id=uuid.uuid4().hex[:12],
            symbol=symbol.upper(),
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            entry_time=_utcnow(),
            setup=setup,
            notes=notes,
        )
        self.trades[record.trade_id] = record
        self.save()
        return record

    def close_trade(self, trade_id: str, exit_price: float, notes: str = "") -> TradeRecord:
        record = self.trades[trade_id]
        record.exit_price = exit_price
        record.exit_time = _utcnow()
        direction = 1.0 if record.side == "long" else -1.0
        record.pnl = direction * (exit_price - record.entry_price) * record.quantity
        if record.entry_price:
            record.pnl_pct = direction * (exit_price / record.entry_price - 1.0) * 100.0
        if notes:
            record.notes = (record.notes + " | " + notes).strip(" |")
        self.save()
        return record

    def open_position_for(self, symbol: str) -> Optional[TradeRecord]:
        for record in self.trades.values():
            if record.symbol == symbol.upper() and record.is_open:
                return record
        return None

    # -- learning from mistakes ---------------------------------------------

    def setup_stats(self) -> Dict[str, Dict[str, float]]:
        """Per-setup performance: trades, win rate, total and average PnL."""
        stats: Dict[str, Dict[str, float]] = {}
        for record in self.trades.values():
            if record.is_open or record.pnl is None:
                continue
            bucket = stats.setdefault(
                record.setup,
                {"trades": 0, "wins": 0, "total_pnl": 0.0, "avg_pnl": 0.0, "win_rate": 0.0},
            )
            bucket["trades"] += 1
            bucket["total_pnl"] += record.pnl
            if record.pnl > 0:
                bucket["wins"] += 1
        for bucket in stats.values():
            bucket["avg_pnl"] = bucket["total_pnl"] / bucket["trades"]
            bucket["win_rate"] = bucket["wins"] / bucket["trades"]
        return stats

    def losing_setups(self, min_trades: int = MIN_TRADES_FOR_LESSON) -> List[str]:
        """Setups with enough history and a negative expectancy -- the desk
        refuses to take these again until their stats improve."""
        return [
            setup
            for setup, s in self.setup_stats().items()
            if s["trades"] >= min_trades and s["avg_pnl"] < 0
        ]

    def should_avoid(self, setup: str, min_trades: int = MIN_TRADES_FOR_LESSON) -> bool:
        return setup in self.losing_setups(min_trades=min_trades)

    def trades_opened_today(self) -> int:
        """Entries opened today (UTC) -- scalper discipline's trade cap.

        Records tagged 'admin' (canceled never-filled orders, account
        migrations) are bookkeeping, not trading activity, and don't count.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        return sum(
            1
            for t in self.trades.values()
            if t.entry_time.startswith(today) and "admin" not in t.tags
        )

    def day_trades_last_5_days(self) -> int:
        """Count same-day round trips in the last 5 calendar days (PDT guard)."""
        count = 0
        now = datetime.now(timezone.utc)
        for record in self.trades.values():
            if record.is_open or not record.exit_time:
                continue
            entry = datetime.fromisoformat(record.entry_time)
            exit_ = datetime.fromisoformat(record.exit_time)
            if entry.date() == exit_.date() and (now - exit_).days <= 5:
                count += 1
        return count

    def summary(self) -> str:
        closed = [t for t in self.trades.values() if not t.is_open]
        open_ = [t for t in self.trades.values() if t.is_open]
        lines = [
            f"Trades: {len(closed)} closed, {len(open_)} open",
        ]
        if closed:
            total = sum(t.pnl or 0.0 for t in closed)
            wins = sum(1 for t in closed if (t.pnl or 0) > 0)
            lines.append(f"Total PnL: {total:+.2f} | Win rate: {wins / len(closed):.0%}")
        stats = self.setup_stats()
        if stats:
            lines.append("Per-setup performance:")
            for setup, s in sorted(stats.items(), key=lambda kv: kv[1]["avg_pnl"]):
                lines.append(
                    f"  {setup}: {int(s['trades'])} trades, "
                    f"win rate {s['win_rate']:.0%}, avg PnL {s['avg_pnl']:+.2f}"
                )
        avoided = self.losing_setups()
        if avoided:
            lines.append("Lessons learned (setups now blocked): " + ", ".join(avoided))
        return "\n".join(lines)
