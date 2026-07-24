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
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from bots.paths import data_path

# A setup needs at least this many closed trades before its stats are trusted
# enough to veto new entries.
MIN_TRADES_FOR_LESSON = 5


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def trading_day(dt: Optional[datetime] = None) -> str:
    """The *trading day* a timestamp belongs to, as an ISO date string.

    Forex desks and funded-account firms roll the trading day at 5pm New
    York time (the daily close), not at UTC midnight. Session 22 found the
    difference matters in practice: with a UTC-midnight boundary the desk
    burned its entire daily trade budget in the overnight Asian session
    (00:30-07:00 UTC) and was then locked out of the London/NY overlap --
    the best liquidity window of the whole day, per its own session-9
    research. Anything after 5pm ET belongs to the NEXT trading day, which
    is exactly how prop firms count 'daily' loss limits too.
    """
    from zoneinfo import ZoneInfo

    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ny = dt.astimezone(ZoneInfo("America/New_York"))
    return (ny + timedelta(hours=7)).date().isoformat()


def _entry_trading_day(iso_ts: str) -> str:
    try:
        return trading_day(datetime.fromisoformat(iso_ts))
    except Exception:
        return iso_ts[:10]  # malformed timestamp: fall back to its date prefix


def risk_of_ruin(win_rate: float, risk_per_trade_pct: float) -> float:
    """Approximate probability of eventually blowing the account, using the
    classic even-money gambler's-ruin formula from trading risk-management
    literature (Van Tharp / Ralph Vince): edge = 2*win_rate - 1,
    RoR = ((1-edge)/(1+edge)) ** (1/risk_per_trade_pct).

    This assumes roughly similar-sized wins and losses (a 1:1 payoff) --
    it's a rough back-of-envelope estimate real desks use before trusting a
    sizing scheme, not an exact number for a strategy with an asymmetric
    reward:risk shape. A negative edge (win_rate <= 50%) returns 1.0:
    ruin is a "when", not an "if", at any sizing.
    """
    if risk_per_trade_pct <= 0:
        return 0.0
    edge = 2 * win_rate - 1
    if edge <= 0:
        return 1.0
    z = (1 - edge) / (1 + edge)
    units = 1.0 / risk_per_trade_pct
    return z ** units


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
    # per-trade stop distance (fraction of entry). None -> the desk's fixed
    # config stop applies. Set by ATR-based sizing so volatile instruments
    # (gold) get wider stops than calm ones (SPY), with size scaled down to
    # keep dollar risk identical.
    stop_pct: Optional[float] = None

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
        stop_pct: Optional[float] = None,
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
            stop_pct=stop_pct,
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
        if record.pnl is not None and record.pnl < 0 and "admin" not in record.tags:
            self._log_mistake(record)
        return record

    def _log_mistake(self, record: TradeRecord) -> None:
        """Plain-text, human-readable trail of every losing trade, written
        the instant it closes -- no chat, no LLM call, just the desk
        journaling its own mistake so there's something to read without
        anyone having to ask. The real self-correction (should_avoid(),
        the Q-agent's negative reward) already happens automatically too;
        this is the readable record of it, not the mechanism."""
        path = data_path("mistakes_log.md")
        line = (
            f"- {(record.exit_time or '')[:16]} **{record.symbol}** {record.side} "
            f"{record.pnl:+.2f} ({record.pnl_pct:+.2f}%) setup=`{record.setup}` "
            f"-- {record.notes}\n"
        )
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass  # logging a mistake should never itself break a trade close

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

    def performance_metrics(self) -> Dict[str, float]:
        """Account-level metrics that matter more than win rate alone:

        - profit_factor: gross profit / gross loss. Above 1 = net positive;
          a 40%-win-rate system with big winners can still beat a 70%-win-rate
          system with big losers.
        - expectancy: average $ per trade -- the number that actually decides
          whether trading more makes you richer or poorer.
        - max_drawdown: worst peak-to-trough dip in cumulative PnL, in the
          order trades closed. The "can you actually survive this" number.
        """
        closed = sorted(
            (t for t in self.trades.values() if not t.is_open and t.pnl is not None),
            key=lambda t: t.exit_time or "",
        )
        if not closed:
            return {}
        gross_profit = sum(t.pnl for t in closed if t.pnl > 0)
        gross_loss = -sum(t.pnl for t in closed if t.pnl < 0)
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in closed:
            cumulative += t.pnl
            peak = max(peak, cumulative)
            max_dd = max(max_dd, peak - cumulative)
        wins = sum(1 for t in closed if t.pnl > 0)
        return {
            "trades": len(closed),
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
            "expectancy": sum(t.pnl for t in closed) / len(closed),
            "max_drawdown": max_dd,
            "win_rate": wins / len(closed),
        }

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

    def payout_readiness(
        self,
        min_payout: float = 100.0,
        max_day_share: float = 0.10,
        min_days_since_first: int = 14,
        min_trading_days: int = 5,
    ) -> Dict[str, object]:
        """Grade this account's journal against typical prop-firm payout
        gates (Clarity Instant defaults: request every 14 days from first
        trade, $100 minimum, no single day over ~10% of the requested
        amount, minimum trading days). The bot cannot press the withdraw
        button -- that is the account owner's dashboard + wallet -- but it
        CAN know the moment the money is requestable and say so."""
        from datetime import datetime, timezone

        closed = [
            t for t in self.trades.values()
            if not t.is_open and t.pnl is not None and "admin" not in t.tags
        ]
        profit = sum(t.pnl for t in closed)
        entries = [t.entry_time for t in self.trades.values() if "admin" not in t.tags]
        days_since_first = 0
        if entries:
            first = datetime.fromisoformat(min(entries))
            if first.tzinfo is None:
                first = first.replace(tzinfo=timezone.utc)
            days_since_first = (datetime.now(timezone.utc) - first).days
        trading_days = len({
            _entry_trading_day(t.entry_time)
            for t in self.trades.values() if "admin" not in t.tags
        })
        day_pnl: Dict[str, float] = {}
        for t in closed:
            day = _entry_trading_day(t.exit_time or t.entry_time)
            day_pnl[day] = day_pnl.get(day, 0.0) + t.pnl
        best_day = max(day_pnl.values()) if day_pnl else 0.0
        day_share = (best_day / profit) if profit > 0 else 0.0

        blockers = []
        if profit < min_payout:
            blockers.append(f"profit ${profit:.2f} below the ${min_payout:.0f} minimum")
        if days_since_first < min_days_since_first:
            blockers.append(
                f"only {days_since_first} days since first trade (need {min_days_since_first})"
            )
        if trading_days < min_trading_days:
            blockers.append(f"only {trading_days} trading days (need {min_trading_days})")
        if profit > 0 and day_share > max_day_share:
            blockers.append(
                f"best day is {day_share:.0%} of profit (consistency cap {max_day_share:.0%}) "
                "-- keep grinding smaller days until it dilutes"
            )
        return {
            "eligible": not blockers,
            "blockers": blockers,
            "profit": profit,
            "days_since_first_trade": days_since_first,
            "trading_days": trading_days,
            "best_day_share": day_share,
        }

    def symbol_stats(self, symbol: str) -> Dict[str, float]:
        """Closed-trade record for one symbol: {'trades', 'pnl', 'win_rate'}.
        Admin records (never-filled orders, migrations) excluded. This is
        the per-SYMBOL version of the setup grading above -- the journal
        judging not just how a strategy performs, but where."""
        key = symbol.upper().strip()
        closed = [
            t for t in self.trades.values()
            if not t.is_open and t.pnl is not None
            and t.symbol.upper().strip() == key and "admin" not in t.tags
        ]
        if not closed:
            return {"trades": 0, "pnl": 0.0, "win_rate": 0.0}
        wins = sum(1 for t in closed if t.pnl > 0)
        return {
            "trades": len(closed),
            "pnl": sum(t.pnl for t in closed),
            "win_rate": wins / len(closed),
        }

    def minutes_since_last_loss(self, symbol: str) -> Optional[float]:
        """Minutes since this symbol's most recent losing close (None if it
        has never lost). Powers the re-entry cooldown: the same signal that
        just got stopped out usually still 'looks good' one cycle later --
        that re-entry is revenge trading with extra steps.

        A genuine breakeven-stop exit is excluded even when its realized
        pnl lands marginally negative from spread/slippage at the fill
        (session 49, user directive: "if it hits breakeven it goes out,
        like I didn't go in the trade at all... it hops back in"). The
        setup still has to clear every other filter to re-enter -- this
        only stops a real-money-neutral exit from silently eating the
        30-minute revenge-trading cooldown meant for actual losers.
        """
        key = symbol.upper().strip()
        losses = [
            t.exit_time for t in self.trades.values()
            if not t.is_open and t.pnl is not None and t.pnl < 0
            and t.symbol.upper().strip() == key and "admin" not in t.tags
            and t.exit_time and "breakeven stop hit" not in (t.notes or "")
        ]
        if not losses:
            return None
        last = max(losses)
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(last)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
        except ValueError:
            return None

    def trades_opened_today(self) -> int:
        """Entries opened today (UTC) -- scalper discipline's trade cap.

        Records tagged 'admin' (canceled never-filled orders, account
        migrations) are bookkeeping, not trading activity, and don't count.
        """
        today = trading_day()
        return sum(
            1
            for t in self.trades.values()
            if _entry_trading_day(t.entry_time) == today and "admin" not in t.tags
        )

    def count_trades_with_tag_today(self, tag: str) -> int:
        """How many of today's entries carry a given tag -- used to cap
        the high-conviction daily-trade-cap override at a fixed number
        per day (session 21), so it stays a narrow exception, not a hole."""
        today = trading_day()
        return sum(
            1
            for t in self.trades.values()
            if _entry_trading_day(t.entry_time) == today and tag in t.tags
        )

    def consecutive_losses_today(self) -> int:
        """Trailing streak of losing closed trades this trading day (5pm-ET
        roll, see trading_day()), newest first.

        The "2-loss rule": after consecutive losses, judgment (human or
        model-driven) degrades and revenge-trading risk spikes -- the desk
        stops opening trades for the day once the streak hits the cap.
        Admin-tagged records don't count.
        """
        today = trading_day()
        closed_today = sorted(
            (
                t for t in self.trades.values()
                if not t.is_open and t.pnl is not None
                and _entry_trading_day(t.exit_time or "") == today
                and "admin" not in t.tags
            ),
            key=lambda t: t.exit_time or "",
            reverse=True,
        )
        streak = 0
        for t in closed_today:
            if t.pnl < 0:
                streak += 1
            else:
                break
        return streak

    def consecutive_wins_today(self) -> int:
        """Trailing streak of winning closed trades this trading day,
        newest first -- the mirror of consecutive_losses_today (session 49,
        user directive: "if I win 2 trades back to back, you're done
        trading"). A breakeven-stop close (pnl ~ 0, not a real win) breaks
        the streak the same way it would break a loss streak: neither a
        loss nor a win, so it can't extend either one."""
        today = trading_day()
        closed_today = sorted(
            (
                t for t in self.trades.values()
                if not t.is_open and t.pnl is not None
                and _entry_trading_day(t.exit_time or "") == today
                and "admin" not in t.tags
            ),
            key=lambda t: t.exit_time or "",
            reverse=True,
        )
        streak = 0
        for t in closed_today:
            if t.pnl > 0:
                streak += 1
            else:
                break
        return streak

    def last_closed_trade(self) -> Optional[TradeRecord]:
        """Most recently closed trade (any day), or None if there isn't one.
        Admin-tagged records (canceled orders, account migrations) don't
        count -- same exclusion as consecutive_losses_today."""
        closed = [
            t for t in self.trades.values()
            if not t.is_open and t.pnl is not None and t.exit_time
            and "admin" not in t.tags
        ]
        if not closed:
            return None
        return max(closed, key=lambda t: t.exit_time or "")

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
            m = self.performance_metrics()
            pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
            lines.append(
                f"Profit factor: {pf} (>1 is net positive) | "
                f"Expectancy: {m['expectancy']:+.2f}/trade | "
                f"Max drawdown: {m['max_drawdown']:.2f}"
            )
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
