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
from bots.risk import DrawdownGuard, MaxDrawdownGuard


@dataclass
class DeskConfig:
    max_positions: int = 5
    max_position_pct: float = 0.15  # max 15% of equity per position
    risk_per_trade_pct: float = 0.01  # risk at most 1% of equity per trade
    max_daily_loss_pct: float = 0.05  # circuit breaker: stop after -5% on the day
    stop_loss_pct: float = 0.05  # exit at -5%
    take_profit_pct: float = 0.15  # exit at +15%
    min_copy_score: float = 2.0  # require at least ~2 independent sources
    pdt_equity_min: float = 25_000.0  # US pattern-day-trader rule threshold
    use_llm_committee: bool = False  # run TradingAgents graph per candidate
    llm_trade_date: Optional[str] = None  # YYYY-MM-DD; defaults to today
    timeframe: str = "1d"  # candle size for signals: "1d" swing, "5m"/"15m" day trading
    day_trading: bool = False  # if True, autopilot flattens all positions before close
    max_trades_per_day: int = 0  # scalper discipline: cap entries per day (0 = no cap)
    news_blackout: bool = True  # no new entries +/-10min around high-impact USD news
    news_currencies: tuple = ("USD",)
    max_per_correlation_group: int = 2  # cap positions per correlated cluster
    breakeven_at_1r: bool = True  # once +1R, stop moves to entry (risk-free trade)
    max_consecutive_losses: int = 3  # "loss-streak rule": stop entering after N straight losses today (0 = off)
    atr_stops: bool = False  # volatility-adaptive stops: 1.5x ATR(14) instead of fixed %
    max_total_drawdown_pct: float = 0.0  # 0 = off; e.g. 0.05 = funded-account 5% max loss limit


def funded_account_config(**overrides) -> "DeskConfig":
    """Preset matching a typical funded/prop-firm rule sheet: 3% daily loss
    limit, 5% max total drawdown, day-trading with ATR stops and the
    loss-streak/news/correlation guards all on. Pass overrides to match
    your specific firm's numbers exactly."""
    base = dict(
        max_daily_loss_pct=0.03,
        max_total_drawdown_pct=0.05,
        day_trading=True,
        timeframe="5m",
        stop_loss_pct=0.015,
        take_profit_pct=0.03,
        max_trades_per_day=10,
        max_consecutive_losses=2,
        atr_stops=True,
        max_per_correlation_group=2,
        news_blackout=True,
        breakeven_at_1r=True,
    )
    base.update(overrides)
    return DeskConfig(**base)


# Correlated clusters: N positions inside one cluster are effectively ONE
# bet at N-times size ("hidden leverage"), because these names move together
# intraday. The desk caps entries per cluster.
CORRELATION_GROUPS = {
    "us-tech": {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META",
                "TSLA", "AVGO", "AMD", "QQQ", "NQ=F"},
    "us-broad": {"SPY", "DIA", "IWM", "ES=F", "YM=F"},
    "gold": {"GC=F", "GLD", "IAU", "XAUUSD"},
    "oil": {"CL=F", "USO", "XLE"},
    "usd-fx": {"EURUSD", "GBPUSD", "USDJPY", "EUR_USD", "GBP_USD", "USD_JPY"},
}


def atr_pct(df, period: int = 14) -> Optional[float]:
    """ATR(14) as a fraction of the last close, or None if the data can't
    support it (missing high/low columns or too few bars)."""
    try:
        cols = {str(c).lower(): c for c in df.columns}
        if "high" not in cols or "low" not in cols or "close" not in cols:
            return None
        high, low, close = df[cols["high"]], df[cols["low"]], df[cols["close"]]
        if len(close) < period + 1:
            return None
        prev_close = close.shift(1)
        import numpy as np

        # element-wise max without pd.concat (concat compares df.attrs
        # across frames, which breaks on frames carrying non-trivial attrs)
        true_range = np.maximum(
            high - low, np.maximum((high - prev_close).abs(), (low - prev_close).abs())
        )
        atr = float(true_range.rolling(period).mean().iloc[-1])
        last = float(close.iloc[-1])
        if not (atr > 0 and last > 0):
            return None
        return atr / last
    except Exception:
        return None


def correlation_group(symbol: str) -> Optional[str]:
    symbol = symbol.upper()
    for name, members in CORRELATION_GROUPS.items():
        if symbol in members:
            return name
    return None


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
        guard: Optional[DrawdownGuard] = None,
        manual_signals_path: Optional[str] = None,
        max_drawdown_guard: Optional[MaxDrawdownGuard] = None,
    ):
        self.broker = broker or PaperBroker()
        self.journal = journal or TradeJournal()
        self.agent = agent or self._load_agent()
        self.config = config or DeskConfig()
        # history_fn(symbol) -> OHLCV DataFrame; injectable for offline tests
        self.history_fn = history_fn or (
            lambda symbol: _default_history(symbol, timeframe=self.config.timeframe)
        )
        # The daily-loss baseline is equity-scale-specific, so it's namespaced
        # per broker -- otherwise switching brokers (e.g. a $10k local paper
        # account to a $100k Alpaca paper account) reads yesterday's baseline
        # from a completely different equity scale and trips the breaker on
        # a false "-900% drawdown".
        from bots.paths import data_path

        self.guard = guard or DrawdownGuard(
            max_daily_loss_pct=self.config.max_daily_loss_pct,
            state_path=data_path(f"day_state_{self.broker.name}.json"),
        )
        self.max_drawdown_guard = max_drawdown_guard or (
            MaxDrawdownGuard(
                max_total_drawdown_pct=self.config.max_total_drawdown_pct,
                state_path=data_path(f"max_drawdown_state_{self.broker.name}.json"),
            )
            if self.config.max_total_drawdown_pct > 0
            else None
        )
        from bots.copytrader import manual as manual_mod

        self.manual_signals_path = manual_signals_path or manual_mod.default_signals_path()

    @staticmethod
    def _load_agent() -> QTraderAgent:
        agent = QTraderAgent()
        agent.load()
        return agent

    # -- research desk ---------------------------------------------------------

    def research_candidates(self, symbols: Optional[List[str]] = None) -> Dict[str, Dict[str, str]]:
        """Buy candidates as {symbol: {"reason": ..., "setup": ...}}.

        Sources, in priority order: manual mirror signals (trades you recorded
        from a human you follow), then the user watchlist, then the automatic
        smart-money consensus.
        """
        from bots.copytrader import manual as manual_mod

        candidates: Dict[str, Dict[str, str]] = {}
        for signal in manual_mod.pending_signals(self.manual_signals_path):
            if signal.side == "buy":
                candidates[signal.symbol] = {
                    "reason": f"mirror call from '{signal.source}' {signal.note}".strip(),
                    "setup": signal.setup,
                    "manual": "1",
                }

        if symbols:
            for s in symbols:
                candidates.setdefault(
                    s.upper(), {"reason": "user watchlist", "setup": "copytrade"}
                )
            return candidates

        from bots.copytrader import consensus_signals

        try:
            for signal in consensus_signals():
                if signal.score >= self.config.min_copy_score:
                    candidates.setdefault(
                        signal.symbol,
                        {"reason": signal.describe(), "setup": "copytrade"},
                    )
        except Exception as exc:
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

        # 0. Reconcile: journal entries whose position vanished at the broker
        #    (an exchange-side bracket stop/target filled between cycles, or a
        #    manual close in the broker app) get closed at the current price.
        self._reconcile_closed_positions(positions, report)

        # 1. Manage existing positions first (risk desk owns exits), honoring
        #    any manual mirror "sell" calls you recorded.
        manual_sells = self._manual_sell_symbols()
        for symbol, quantity in list(positions.items()):
            self._manage_position(symbol, quantity, report, force_exit=symbol in manual_sells)

        # 2. Daily-loss circuit breaker: after -max_daily_loss_pct on the day,
        #    no new entries until tomorrow (exits above still ran).
        halted, guard_msg = self.guard.check(self.broker.equity())
        report.notes.append(f"[risk] {guard_msg}")
        if halted:
            return report

        if self.max_drawdown_guard is not None:
            max_dd_halted, max_dd_msg = self.max_drawdown_guard.check(self.broker.equity())
            report.notes.append(f"[risk] {max_dd_msg}")
            if max_dd_halted:
                return report

        # 2b. News blackout: high-impact releases (NFP/CPI/FOMC) blow spreads
        #     out and are hard-prohibited windows on most funded accounts.
        #     Exits above already ran; only new entries are blocked.
        if cfg.news_blackout:
            if not hasattr(self, "_news_guard"):
                from bots.newsguard import NewsGuard

                self._news_guard = NewsGuard(currencies=cfg.news_currencies)
            news_blocked, news_msg = self._news_guard.blackout()
            report.notes.append(f"[news] {news_msg}")
            if news_blocked:
                return report

        # 2c. Loss-streak rule: consecutive losses today mean something is
        #     off (regime shift, bad model day) -- stop entering, keep exits.
        if cfg.max_consecutive_losses > 0:
            streak = self.journal.consecutive_losses_today()
            if streak >= cfg.max_consecutive_losses:
                report.notes.append(
                    f"[risk] loss-streak rule: {streak} consecutive losses today "
                    f"(cap {cfg.max_consecutive_losses}) -- no new entries until tomorrow"
                )
                return report

        # 3. New entries from research + quant + committee, filtered by lessons.
        # A "slot" is used by a filled position OR a trade the journal still
        # considers open (which includes orders placed but not yet filled --
        # e.g. a stock order queued outside market hours). Without counting
        # those, max_positions only caps settled positions and a second cycle
        # run before the first batch fills can commit well past the cap.
        candidates = self.research_candidates(symbols)
        committed_symbols = set(positions) | {
            t.symbol for t in self.journal.trades.values() if t.is_open
        }
        open_slots = cfg.max_positions - len(committed_symbols)
        trades_left_today = (
            cfg.max_trades_per_day - self.journal.trades_opened_today()
            if cfg.max_trades_per_day > 0
            else None
        )
        group_counts: Dict[str, int] = {}
        for held in committed_symbols:
            group = correlation_group(held)
            if group:
                group_counts[group] = group_counts.get(group, 0) + 1
        for symbol, info in candidates.items():
            if trades_left_today is not None and trades_left_today <= 0:
                report.actions.append(
                    DeskAction("skip", symbol, "scalper discipline: daily trade cap reached")
                )
                continue
            if symbol in committed_symbols:
                continue
            if open_slots <= 0:
                report.actions.append(
                    DeskAction("skip", symbol, "risk desk: max positions reached")
                )
                continue
            if self.broker.has_pending_order(symbol):
                report.actions.append(
                    DeskAction("skip", symbol, "order already pending fill, not resubmitting")
                )
                continue
            group = correlation_group(symbol)
            if (
                cfg.max_per_correlation_group > 0
                and group
                and group_counts.get(group, 0) >= cfg.max_per_correlation_group
            ):
                report.actions.append(
                    DeskAction(
                        "skip", symbol,
                        f"correlation guard: already {group_counts[group]} positions in "
                        f"the '{group}' cluster -- more would be hidden leverage on one bet",
                    )
                )
                continue
            action = self._consider_entry(
                symbol, info["reason"], equity,
                setup_base=info.get("setup", "copytrade"),
                manual=bool(info.get("manual")),
            )
            report.actions.append(action)
            if action.action == "buy" and action.ok:
                open_slots -= 1
                if trades_left_today is not None:
                    trades_left_today -= 1
                if group:
                    group_counts[group] = group_counts.get(group, 0) + 1
        return report

    def flatten_all(self, reason: str = "day-trading: flatten before close") -> DeskReport:
        """Close every open position. Real day traders don't hold overnight --
        overnight moves aren't covered by the intraday stop-loss/take-profit
        checks, so autopilot calls this near market close when day_trading
        is on."""
        report = DeskReport()
        for symbol, quantity in list(self.broker.positions().items()):
            self._manage_position(
                symbol, quantity, report, force_exit=True, force_exit_reason=reason
            )
        return report

    def _reconcile_closed_positions(self, positions: Dict[str, float], report: DeskReport) -> None:
        for record in list(self.journal.trades.values()):
            if not record.is_open or record.symbol in positions:
                continue
            if self.broker.has_pending_order(record.symbol):
                continue  # entry order not filled yet, not a closed position
            try:
                price = self.broker.price(record.symbol)
            except Exception:
                continue
            closed = self.journal.close_trade(
                record.trade_id, price,
                notes="reconciled: closed at broker (bracket stop/target or manual)",
            )
            report.actions.append(
                DeskAction(
                    "sell", record.symbol,
                    f"bracket/manual exit reconciled | realized PnL {closed.pnl:+.2f}",
                    quantity=record.quantity,
                )
            )

    def _manual_sell_symbols(self) -> set:
        from bots.copytrader import manual as manual_mod

        return {
            s.symbol
            for s in manual_mod.pending_signals(self.manual_signals_path)
            if s.side == "sell"
        }

    def _manage_position(
        self,
        symbol: str,
        quantity: float,
        report: DeskReport,
        force_exit: bool = False,
        force_exit_reason: str = "manual mirror call says exit",
    ) -> None:
        cfg = self.config
        record = self.journal.open_position_for(symbol)
        try:
            price = self.broker.price(symbol)
        except Exception as exc:
            report.actions.append(DeskAction("hold", symbol, f"no price data: {exc}"))
            return

        reason = None
        if force_exit:
            reason = force_exit_reason
        elif record:
            change = price / record.entry_price - 1.0
            # ATR-sized trades carry their own stop distance; 1:2 R:R shape
            # is preserved by scaling the target with it.
            stop_pct = record.stop_pct or cfg.stop_loss_pct
            target_pct = 2.0 * record.stop_pct if record.stop_pct else cfg.take_profit_pct
            breakeven_armed = "breakeven-armed" in record.tags
            # Once the trade is up 1R (one stop-distance), the worst case
            # becomes "out at entry" instead of a loss -- the trade is
            # risk-free from here (standard professional trade management).
            if cfg.breakeven_at_1r and not breakeven_armed and change >= stop_pct:
                record.tags.append("breakeven-armed")
                self.journal.save()
                breakeven_armed = True
                report.notes.append(
                    f"[manage] {symbol} reached +1R ({change:+.1%}) -- stop moved to breakeven"
                )
            if change <= -stop_pct:
                reason = f"stop loss hit ({change:+.1%})"
            elif breakeven_armed and change <= 0.0:
                reason = f"breakeven stop hit (was +1R, now {change:+.1%}) -- risk-free exit"
            elif change >= target_pct:
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
        if result.ok and force_exit:
            from bots.copytrader import manual as manual_mod

            manual_mod.consume_signal(symbol, self.manual_signals_path)
        report.actions.append(
            DeskAction("sell", symbol, reason, quantity=quantity, ok=result.ok)
        )

    def _consider_entry(
        self,
        symbol: str,
        reason: str,
        equity: float,
        setup_base: str = "copytrade",
        manual: bool = False,
    ) -> DeskAction:
        cfg = self.config

        # Quant desk vote (needs history; a missing feed means no veto). Manual
        # mirror calls carry the caller's setup tag so the journal can grade
        # that human's real performance separately.
        setup_suffix = ""
        symbol_atr = None
        try:
            df = self.history_fn(symbol)
            vote = self.agent.signal(df, holding=False)
            if not manual:
                setup_suffix = ":" + self.agent.current_state(df, holding=False)
            if vote == "sell" and not manual:
                return DeskAction("skip", symbol, "quant desk: RL agent is bearish here")
            if cfg.atr_stops:
                symbol_atr = atr_pct(df)
        except Exception:
            pass

        setup = f"{setup_base}{setup_suffix}"

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

        # Position sizing, scalper style: risk at most risk_per_trade_pct of
        # equity if the stop-loss gets hit, and never more than
        # max_position_pct of equity in one name regardless. With atr_stops
        # on, the stop distance adapts to the instrument's volatility
        # (1.5x ATR14, clamped) and the size shrinks in proportion, so a
        # gold trade and an SPY trade risk the same dollars.
        stop_pct = cfg.stop_loss_pct
        take_profit_pct = cfg.take_profit_pct
        record_stop = None
        if cfg.atr_stops and symbol_atr:
            stop_pct = min(max(1.5 * symbol_atr, 0.003), 0.05)
            take_profit_pct = 2.0 * stop_pct  # keep the 1:2 risk:reward shape
            record_stop = stop_pct
        risk_budget = equity * cfg.risk_per_trade_pct / max(stop_pct, 1e-6)
        budget = min(risk_budget, equity * cfg.max_position_pct, self.broker.cash())
        quantity = round(budget / price, 4) if price > 0 else 0.0
        if quantity <= 0:
            return DeskAction("skip", symbol, "risk desk: no budget available")

        result = self.broker.buy_bracket(symbol, quantity, stop_pct, take_profit_pct)
        if result.ok:
            self.journal.open_trade(
                symbol, "long", result.quantity or quantity,
                result.fill_price or price, setup=setup, notes=reason,
                stop_pct=record_stop,
            )
            if manual:
                from bots.copytrader import manual as manual_mod

                manual_mod.consume_signal(symbol, self.manual_signals_path)
        return DeskAction(
            "buy", symbol,
            reason if result.ok else f"order failed: {result.error}",
            quantity=quantity, ok=result.ok,
        )


def _default_history(symbol: str, timeframe: str = "1d"):
    from bots import marketdata

    # Yahoo limits how far back intraday candles go; daily/weekly can go
    # back much further, which the RL agent's SMA30 needs to warm up.
    period = "6mo" if timeframe in ("1d", "1wk", "1mo") else "5d"
    return marketdata.get_history(symbol, period=period, interval=timeframe)
