"""TradingDesk: runs the bots like a small trading organization.

Desks and their real-world equivalents:

- Research desk   -> bots.copytrader consensus signals (what profitable,
                     publicly-tracked investors are buying)
- Quant desk      -> bots.learning Q-agent vote from its trained Q-table
- AI committee    -> optional: the TradingAgents LLM analyst/researcher/risk
                     debate already in this repo (needs LLM API keys)
- Risk desk       -> position sizing, max positions, stop-loss/take-profit,
                     pattern-day-trading guard, and journal-based vetoes of
                     setups that historically lost money (learning from
                     mistakes at the organization level)
- Execution desk  -> bots.brokers (paper by default; Alpaca/Robinhood/crypto)

Every decision is written to the TradeJournal so future runs get smarter about
which setups to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from bots.brokers import Broker, PaperBroker
from bots.journal import TradeJournal
from bots.learning import QTraderAgent


@dataclass
class DeskConfig:
    max_positions: int = 5
    max_position_pct: float = 0.15  # max 15% of equity per position
    stop_loss_pct: float = 0.05  # exit at -5%
    take_profit_pct: float = 0.15  # exit at +15%
    min_copy_score: float = 2.0  # require at least ~2 independent sources
    pdt_equity_min: float = 25_000.0  # US pattern-day-trader rule threshold
    use_llm_committee: bool = False  # run TradingAgents graph per candidate
    llm_trade_date: Optional[str] = None  # YYYY-MM-DD; defaults to today


@dataclass
class DeskAction:
    action: str  # "buy" / "sell" / "skip" / "hold"
    symbol: str
    reason: str
    quantity: float = 0.0
    ok: bool = True


@dataclass
class DeskReport:
    actions: List[DeskAction] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = list(self.notes)
        for act in self.actions:
            status = "" if act.ok else " [FAILED]"
            qty = f" x{act.quantity:g}" if act.quantity else ""
            lines.append(f"{act.action.upper()} {act.symbol}{qty}{status}: {act.reason}")
        return "\n".join(lines) or "No actions this cycle."


class TradingDesk:
    def __init__(
        self,
        broker: Optional[Broker] = None,
        journal: Optional[TradeJournal] = None,
        agent: Optional[QTraderAgent] = None,
        config: Optional[DeskConfig] = None,
        history_fn: Optional[Callable[[str], "object"]] = None,
    ):
        self.broker = broker or PaperBroker()
        self.journal = journal or TradeJournal()
        self.agent = agent or self._load_agent()
        self.config = config or DeskConfig()
        # history_fn(symbol) -> OHLCV DataFrame; injectable for offline tests
        self.history_fn = history_fn or _default_history

    @staticmethod
    def _load_agent() -> QTraderAgent:
        agent = QTraderAgent()
        agent.load()
        return agent

    # -- research desk ---------------------------------------------------------

    def research_candidates(self, symbols: Optional[List[str]] = None) -> Dict[str, str]:
        """Buy candidates as {symbol: reason}."""
        if symbols:
            return {s.upper(): "user watchlist" for s in symbols}
        from bots.copytrader import consensus_signals

        candidates: Dict[str, str] = {}
        try:
            for signal in consensus_signals():
                if signal.score >= self.config.min_copy_score:
                    candidates[signal.symbol] = signal.describe()
        except Exception as exc:
            candidates = {}
            print(f"[research] copy-trading feeds unavailable: {exc}")
        return candidates

    # -- AI committee (optional, uses the TradingAgents LLM graph) --------------

    def llm_rating(self, symbol: str) -> Optional[str]:
        if not self.config.use_llm_committee:
            return None
        try:
            from datetime import date

            from tradingagents.graph.trading_graph import TradingAgentsGraph

            graph = TradingAgentsGraph(debug=False)
            trade_date = self.config.llm_trade_date or date.today().isoformat()
            _, decision = graph.propagate(symbol, trade_date)
            return decision  # Buy / Overweight / Hold / Underweight / Sell
        except Exception as exc:
            print(f"[committee] LLM rating unavailable for {symbol}: {exc}")
            return None

    # -- one full cycle ----------------------------------------------------------

    def run_once(self, symbols: Optional[List[str]] = None) -> DeskReport:
        report = DeskReport()
        cfg = self.config
        equity = self.broker.equity()
        positions = self.broker.positions()
        report.notes.append(
            f"[desk] broker={self.broker.name} (paper={self.broker.is_paper}) "
            f"equity={equity:.2f} cash={self.broker.cash():.2f} "
            f"positions={list(positions) or 'none'}"
        )

        if not self.broker.is_paper and equity < cfg.pdt_equity_min:
            day_trades = self.journal.day_trades_last_5_days()
            if day_trades >= 3:
                report.notes.append(
                    f"[risk] PDT guard: {day_trades} day trades in 5 days with equity "
                    f"under ${cfg.pdt_equity_min:,.0f} -- skipping new entries this "
                    "cycle (US pattern-day-trading rule)."
                )
                return report

        # 1. Manage existing positions first (risk desk owns exits).
        for symbol, quantity in list(positions.items()):
            self._manage_position(symbol, quantity, report)

        # 2. New entries from research + quant + committee, filtered by lessons.
        candidates = self.research_candidates(symbols)
        open_slots = cfg.max_positions - len(self.broker.positions())
        for symbol, reason in candidates.items():
            if open_slots <= 0:
                report.actions.append(
                    DeskAction("skip", symbol, "risk desk: max positions reached")
                )
                continue
            if symbol in positions:
                continue
            action = self._consider_entry(symbol, reason, equity)
            report.actions.append(action)
            if action.action == "buy" and action.ok:
                open_slots -= 1
        return report

    def _manage_position(self, symbol: str, quantity: float, report: DeskReport) -> None:
        cfg = self.config
        record = self.journal.open_position_for(symbol)
        try:
            price = self.broker.price(symbol)
        except Exception as exc:
            report.actions.append(DeskAction("hold", symbol, f"no price data: {exc}"))
            return

        reason = None
        if record:
            change = price / record.entry_price - 1.0
            if change <= -cfg.stop_loss_pct:
                reason = f"stop loss hit ({change:+.1%})"
            elif change >= cfg.take_profit_pct:
                reason = f"take profit hit ({change:+.1%})"
        if reason is None:
            try:
                df = self.history_fn(symbol)
                if self.agent.signal(df, holding=True) == "sell":
                    reason = "quant desk: RL agent says exit"
            except Exception:
                pass
        if reason is None:
            report.actions.append(DeskAction("hold", symbol, "within risk limits"))
            return

        result = self.broker.sell(symbol, quantity)
        if result.ok and record:
            closed = self.journal.close_trade(
                record.trade_id, result.fill_price or price, notes=reason
            )
            reason += f" | realized PnL {closed.pnl:+.2f}"
        report.actions.append(
            DeskAction("sell", symbol, reason, quantity=quantity, ok=result.ok)
        )

    def _consider_entry(self, symbol: str, reason: str, equity: float) -> DeskAction:
        cfg = self.config

        # Quant desk vote (needs history; a missing feed means no veto).
        setup_suffix = ""
        try:
            df = self.history_fn(symbol)
            vote = self.agent.signal(df, holding=False)
            setup_suffix = ":" + self.agent.current_state(df, holding=False)
            if vote == "sell":
                return DeskAction("skip", symbol, "quant desk: RL agent is bearish here")
        except Exception:
            pass

        setup = f"copytrade{setup_suffix}"

        # Lessons learned: refuse setups with proven negative expectancy.
        if self.journal.should_avoid(setup):
            return DeskAction(
                "skip", symbol,
                f"journal: setup '{setup}' has lost money historically (lesson learned)",
            )

        rating = self.llm_rating(symbol)
        if rating in ("Sell", "Underweight"):
            return DeskAction("skip", symbol, f"AI committee rated {rating}")

        try:
            price = self.broker.price(symbol)
        except Exception as exc:
            return DeskAction("skip", symbol, f"no price data: {exc}")

        budget = min(equity * cfg.max_position_pct, self.broker.cash())
        quantity = round(budget / price, 4) if price > 0 else 0.0
        if quantity <= 0:
            return DeskAction("skip", symbol, "risk desk: no budget available")

        result = self.broker.buy(symbol, quantity)
        if result.ok:
            self.journal.open_trade(
                symbol, "long", quantity, result.fill_price or price, setup=setup,
                notes=reason,
            )
        return DeskAction(
            "buy", symbol,
            reason if result.ok else f"order failed: {result.error}",
            quantity=quantity, ok=result.ok,
        )


def _default_history(symbol: str):
    import yfinance as yf

    return yf.Ticker(symbol).history(period="6mo")
