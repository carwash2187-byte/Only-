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

import numpy as np
import pandas as pd

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
    daily_profit_target_pct: float = 0.0  # 0 = off; e.g. 0.02: once up 2% on the day, cash out everything and stop
    min_adx: float = 0.0  # regime filter: skip entries when ADX(14) is below this (0 = off; ~20 typical)
    session_aware_forex: bool = False  # skip/deprioritize FX pairs outside their liquid trading session
    htf_confirm: bool = False  # require a higher-timeframe trend filter to agree before entering
    reduce_size_after_loss: bool = False  # anti-martingale: halve risk_per_trade_pct right after a loss
    max_hold_minutes: int = 0  # time stop: exit a trade that hasn't reached +1R after this long (0 = off)
    orb_retest_required: bool = False  # MambaFX-style: don't chase an extended breakout candle early in the session, wait for a retest


def funded_account_config(**overrides) -> "DeskConfig":
    """Preset matching a typical funded/prop-firm rule sheet: 3% daily loss
    limit, 5% max total drawdown, day-trading with ATR stops and the
    loss-streak/news/correlation guards all on. Pass overrides to match
    your specific firm's numbers exactly."""
    base = dict(
        max_daily_loss_pct=0.03,
        max_total_drawdown_pct=0.05,
        # 3% daily cash-out target ($150 on a $5k account) per user choice.
        # Note it sits AT the 3% daily-loss limit's mirror image: a green
        # day now has to run further before it locks in, so expect fewer
        # (but bigger) target-hit days than at 2%.
        daily_profit_target_pct=0.03,
        # Slightly above the 1% generic default: the hard backstops are the
        # 3% daily / 5% total drawdown circuit breakers above, not the
        # per-trade size -- those trip regardless of how any single trade
        # is sized, so this only changes how fast a *winning* day reaches
        # the profit target, not the worst-case loss on a bad one.
        risk_per_trade_pct=0.015,
        reduce_size_after_loss=True,
        min_adx=20.0,
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
        session_aware_forex=True,
        htf_confirm=True,
        # Time stop (session 16): a scalp thesis on 1m/5m candles is stale
        # long before 2 hours -- if the trade hasn't even reached +1R by
        # then, the setup didn't confirm; free the capital and risk budget.
        max_hold_minutes=120,
        # MambaFX's own strategy is explicitly a breakout style; his
        # documented risk practice is retest-based, not chase-based (session
        # 17). Real ORB backtests show 65.9% of raw breakouts hit their
        # stop -- this is the fix.
        orb_retest_required=True,
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


def orb_chase_filter(df, atr_multiple: float = 1.0) -> Optional[str]:
    """Approximates 'wait for the retest, don't chase the breakout candle'
    (session 17, MambaFX-style breakout trading): real backtests show raw
    opening-range breakouts hit their stop 65.9% of the time, mostly from
    chasing an extended candle in the first ~2 hours after the open --
    exactly the window this checks. If price is already more than
    atr_multiple ATRs past today's opening-range high/low without having
    pulled back, that's the chase pattern; the fix (break-and-retest) is
    to wait for the level to be revisited and hold before entering.

    Returns 'chasing' to veto, or None (no opinion -- not early enough in
    the session, not a real single-session intraday market, or not
    enough data to compute an opening range at all). Only checked in the
    early session window on purpose: normal trend-continuation trades
    later in the day are *supposed* to be far from the morning's range,
    that isn't chasing.
    """
    from bots.learning.agent import _is_continuous_market, _is_intraday

    try:
        cols = {str(c).lower(): c for c in df.columns}
        if not {"open", "high", "low", "close"} <= set(cols):
            return None
        if not _is_intraday(df) or len(df) < 40:
            return None
        idx = df.index
        bar_minutes = max(1, int(pd.Series(idx[1:] - idx[:-1]).median().total_seconds() // 60))
        if _is_continuous_market(df, bar_minutes):
            return None  # no real single session to have an "opening range"

        high, low, close = df[cols["high"]], df[cols["low"]], df[cols["close"]]
        session = idx.normalize()
        bar_of_day = pd.Series(1, index=idx).groupby(session).cumcount()
        bars_30min = max(1, round(30 / bar_minutes))
        bars_2h = max(1, round(120 / bar_minutes))
        if int(bar_of_day.iloc[-1]) >= bars_2h:
            return None  # past the early-session chase window

        in_or_window = bar_of_day < bars_30min
        todays_session = session.iloc[-1] if hasattr(session, "iloc") else session[-1]
        in_today = session == todays_session
        window = in_or_window & in_today
        if not window.any():
            return None

        or_high, or_low = float(high[window].max()), float(low[window].min())
        atr = atr_pct(df)
        if atr is None:
            return None
        atr_abs = atr * float(close.iloc[-1])
        if atr_abs <= 0:
            return None
        last_close = float(close.iloc[-1])
        if last_close > or_high + atr_multiple * atr_abs or last_close < or_low - atr_multiple * atr_abs:
            return "chasing"
        return None
    except Exception:
        return None


def adx_value(df, period: int = 14) -> Optional[float]:
    """ADX(14): trend-strength gauge. Below ~20 the market is chopping and
    breakout entries mostly fail; above ~25 it's trending. None when the
    data can't support it (no high/low columns, too few bars)."""
    try:
        cols = {str(c).lower(): c for c in df.columns}
        if "high" not in cols or "low" not in cols or "close" not in cols:
            return None
        high, low, close = df[cols["high"]], df[cols["low"]], df[cols["close"]]
        if len(close) < period * 2 + 1:
            return None
        import numpy as np

        up = high.diff()
        down = -low.diff()
        plus_dm = up.where((up > down) & (up > 0), 0.0)
        minus_dm = down.where((down > up) & (down > 0), 0.0)
        prev_close = close.shift(1)
        tr = np.maximum(
            high - low, np.maximum((high - prev_close).abs(), (low - prev_close).abs())
        )
        atr = tr.rolling(period).mean()
        plus_di = 100 * plus_dm.rolling(period).mean() / atr
        minus_di = 100 * minus_dm.rolling(period).mean() / atr
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
        adx = float(dx.rolling(period).mean().iloc[-1])
        return adx if adx == adx else None  # NaN check
    except Exception:
        return None


def correlation_group(symbol: str) -> Optional[str]:
    symbol = symbol.upper()
    for name, members in CORRELATION_GROUPS.items():
        if symbol in members:
            return name
    return None


# Each currency pair is only genuinely liquid while at least one of its two
# home markets is open. Trading a pair outside its session means wider
# spreads and choppier, less institutional-driven price action -- one of the
# three real losses traced in Session 7 was exactly this (a signal taken in
# a dead session). Pairs list per session per real trading-desk convention;
# "overlap" (London/New York, 8am-12pm ET) is the single highest-liquidity
# window in the whole FX day and gets scored highest.
FOREX_SESSION_PAIRS = {
    "asian": {"USDJPY", "AUDUSD", "NZDUSD", "AUDJPY", "EURJPY", "GBPJPY"},
    "london": {"EURUSD", "GBPUSD", "EURJPY", "EURGBP", "EURCHF", "GBPJPY"},
    "newyork": {"EURUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD"},
    "overlap": {"EURUSD", "GBPUSD", "USDCHF", "USDCAD"},
}


def active_forex_session(now=None) -> str:
    """Which real-world FX session is live right now, by ET hour. Returns
    'overlap' (8am-12pm ET, London+NY both open -- highest liquidity),
    'london' (3am-8am ET), 'newyork' (12pm-5pm ET), or 'asian' (otherwise)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = (now or datetime.now(tz=ZoneInfo("America/New_York"))).astimezone(
        ZoneInfo("America/New_York")
    )
    hour = now.hour
    if 8 <= hour < 12:
        return "overlap"
    if 3 <= hour < 8:
        return "london"
    if 12 <= hour < 17:
        return "newyork"
    return "asian"


# Higher-timeframe confirmation: entries taken on a low timeframe (5m/1m
# scalping) win far more often when a bigger-picture timeframe agrees with
# the direction -- studies cited in session 10 show ~18-23pt win-rate gains
# from this filter alone. Ratio follows the commonly used 4:1-5:1 spacing
# (entry timeframe : confirmation timeframe).
HTF_MAP = {"1m": "15m", "5m": "1h", "15m": "1h", "30m": "4h"}


def heikin_ashi(df):
    """Heikin-Ashi transform: averages each bar with the running trend so
    noise cancels out and the direction reads cleaner. Real caveats from
    the research (session 15): it lags real price (never use it on the
    fast entry timeframe) and works best as a *trend filter* on a higher
    timeframe, not standalone -- which is exactly the HTF-confirm role
    it's used for here, not the entry signal itself."""
    cols = {str(c).lower(): c for c in df.columns}
    o, h, l, c = df[cols["open"]], df[cols["high"]], df[cols["low"]], df[cols["close"]]
    ha_close = (o + h + l + c) / 4.0
    ha_open = ha_close.copy()
    ha_open.iloc[0] = (o.iloc[0] + c.iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2.0
    # np.maximum/minimum chains, not pd.concat -- pandas compares .attrs
    # dicts during concat, which raises on any frame carrying the Q-agent's
    # cached-feature marker (see bots/learning/agent.py's _FeatureCache).
    ha_high = pd.Series(np.maximum(np.maximum(h.values, ha_open.values), ha_close.values), index=df.index)
    ha_low = pd.Series(np.minimum(np.minimum(l.values, ha_open.values), ha_close.values), index=df.index)
    return pd.DataFrame(
        {"open": ha_open, "high": ha_high, "low": ha_low, "close": ha_close}, index=df.index
    )


def trend_direction(df, fast: int = 10, slow: int = 30) -> Optional[str]:
    """SMA-fast vs SMA-slow direction ('up'/'down'), the same trend rule the
    Q-agent's own state features use -- reused here so a higher-timeframe
    confirmation check agrees methodologically with what the agent already
    trained on. None if there isn't enough history to compute it."""
    try:
        cols = {str(c).lower(): c for c in df.columns}
        if "close" not in cols:
            return None
        close = df[cols["close"]]
        if len(close) < slow + 1:
            return None
        f = float(close.rolling(fast).mean().iloc[-1])
        s = float(close.rolling(slow).mean().iloc[-1])
        if f != f or s != s:  # NaN check
            return None
        return "up" if f >= s else "down"
    except Exception:
        return None


def forex_session_score(symbol: str, now=None) -> int:
    """0-2 liquidity score for a currency pair at the current time: 2 if
    it's a core pair for the live overlap window, 1 if it's active in
    whichever single session is live, 0 if it isn't an FX pair or isn't
    trading actively right now. Non-FX symbols always score 0 (this only
    applies to currency pairs)."""
    symbol = symbol.upper().replace("_", "").replace("/", "")
    session = active_forex_session(now)
    if session == "overlap" and symbol in FOREX_SESSION_PAIRS["overlap"]:
        return 2
    if symbol in FOREX_SESSION_PAIRS.get(session, ()):
        return 1
    return 0


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
        htf_history_fn: Optional[Callable[[str, str], "object"]] = None,
    ):
        self.broker = broker or PaperBroker()
        self.journal = journal or TradeJournal()
        self.agent = agent or self._load_agent()
        self.config = config or DeskConfig()
        # history_fn(symbol) -> OHLCV DataFrame; injectable for offline tests
        self.history_fn = history_fn or (
            lambda symbol: _default_history(symbol, timeframe=self.config.timeframe)
        )
        # htf_history_fn(symbol, timeframe) -> OHLCV DataFrame for the
        # higher-timeframe confirmation filter; separate from history_fn
        # because it needs a different timeframe than the entry signal.
        self.htf_history_fn = htf_history_fn or (
            lambda symbol, timeframe: _default_history(symbol, timeframe=timeframe)
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

        # 2a-bis. Daily profit target ("quit while ahead"): once today's gain
        #    hits the target, cash out every position and stop for the day.
        #    Prop-firm practice: the fastest way to fail a funded account
        #    after a good morning is giving the profit back in the afternoon,
        #    and consistency rules punish oversized single days anyway.
        if cfg.daily_profit_target_pct > 0:
            day_gain = self.guard.day_gain_pct(self.broker.equity())
            if day_gain >= cfg.daily_profit_target_pct:
                report.notes.append(
                    f"[risk] daily profit target hit ({day_gain:+.1%} >= "
                    f"{cfg.daily_profit_target_pct:.1%}) -- cashing out, done for the day"
                )
                flatten = self.flatten_all(
                    reason=f"daily profit target hit ({day_gain:+.1%}) -- locking in the day"
                )
                report.actions.extend(flatten.actions)
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

        ordered_candidates = list(candidates.items())
        if cfg.session_aware_forex:
            session = active_forex_session()
            skipped_off_session = [
                symbol for symbol in candidates
                if forex_session_score(symbol) == 0 and correlation_group(symbol) == "usd-fx"
            ]
            for symbol in skipped_off_session:
                report.actions.append(
                    DeskAction(
                        "skip", symbol,
                        f"session filter: '{session}' session is live, {symbol} isn't a "
                        "core pair for it right now (thin liquidity/wide spreads off-session)",
                    )
                )
            ordered_candidates = [
                (s, info) for s, info in ordered_candidates if s not in skipped_off_session
            ]
            ordered_candidates.sort(key=lambda item: -forex_session_score(item[0]))

        for symbol, info in ordered_candidates:
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

    def flatten_all(
        self, reason: str = "day-trading: flatten before close", symbols: Optional[set] = None
    ) -> DeskReport:
        """Close open positions. Real day traders don't hold overnight --
        overnight moves aren't covered by the intraday stop-loss/take-profit
        checks, so autopilot calls this near market close when day_trading
        is on. `symbols`, if given, limits this to just that subset (e.g.
        flattening stock positions at the 4pm close while leaving
        still-tradeable forex/futures positions open)."""
        report = DeskReport()
        for symbol, quantity in list(self.broker.positions().items()):
            if symbols is not None and symbol not in symbols:
                continue
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
            elif cfg.max_hold_minutes > 0 and not breakeven_armed:
                # Time stop: the entry thesis was a fast intraday move. If
                # the trade hasn't even reached +1R after max_hold_minutes,
                # the setup didn't confirm -- exit on the clock, free the
                # capital, defend against "state drift" (the market that
                # exists now isn't the one the signal fired in).
                from datetime import datetime, timezone

                try:
                    entry_dt = datetime.fromisoformat(record.entry_time)
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                    age_min = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60.0
                    if age_min >= cfg.max_hold_minutes:
                        reason = (
                            f"time stop: {age_min:.0f} min in the trade without reaching "
                            f"+1R (cap {cfg.max_hold_minutes}) -- setup went stale ({change:+.1%})"
                        )
                except Exception:
                    pass
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
            if cfg.min_adx > 0 and not manual:
                adx = adx_value(df)
                if adx is not None and adx < cfg.min_adx:
                    return DeskAction(
                        "skip", symbol,
                        f"regime filter: ADX {adx:.0f} < {cfg.min_adx:.0f} -- choppy "
                        "market, breakout entries mostly fail here",
                    )
            if cfg.orb_retest_required and not manual and orb_chase_filter(df) == "chasing":
                return DeskAction(
                    "skip", symbol,
                    "breakout filter: price already ran past today's opening range "
                    "without a pullback -- waiting for a retest instead of chasing",
                )
        except Exception:
            pass

        if cfg.htf_confirm and not manual:
            htf = HTF_MAP.get(cfg.timeframe)
            if htf:
                try:
                    htf_df = self.htf_history_fn(symbol, htf)
                    try:
                        htf_df = heikin_ashi(htf_df)
                    except Exception:
                        pass  # fall back to raw OHLC trend if HA transform fails
                    htf_trend = trend_direction(htf_df)
                    if htf_trend == "down":
                        return DeskAction(
                            "skip", symbol,
                            f"higher-timeframe filter: {htf} trend is down -- a "
                            f"{cfg.timeframe} buy signal against the bigger trend "
                            "wins far less often",
                        )
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
        risk_pct = cfg.risk_per_trade_pct
        sizing_note = ""
        if cfg.reduce_size_after_loss:
            last_trade = self.journal.last_closed_trade()
            if last_trade is not None and last_trade.pnl is not None and last_trade.pnl < 0:
                risk_pct = risk_pct / 2.0
                sizing_note = " (anti-martingale: half size after the last loss)"
        if self.max_drawdown_guard is not None:
            dd_mult = self.max_drawdown_guard.size_multiplier(equity)
            if dd_mult < 1.0:
                risk_pct *= dd_mult
                sizing_note += f" (drawdown taper: {dd_mult:.0%} size, approaching the max-loss ceiling)"
        risk_budget = equity * risk_pct / max(stop_pct, 1e-6)
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
            (reason if result.ok else f"order failed: {result.error}") + (sizing_note if result.ok else ""),
            quantity=quantity, ok=result.ok,
        )


def _default_history(symbol: str, timeframe: str = "1d"):
    from bots import marketdata

    # Yahoo limits how far back intraday candles go; daily/weekly can go
    # back much further, which the RL agent's SMA30 needs to warm up.
    period = "6mo" if timeframe in ("1d", "1wk", "1mo") else "5d"
    return marketdata.get_history(symbol, period=period, interval=timeframe)
