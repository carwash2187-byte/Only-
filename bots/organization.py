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
from bots.risk import ChallengeTargetGuard, DrawdownGuard, MaxDrawdownGuard


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
    news_fail_closed: bool = False  # funded safety: if the news feed can't be verified fresh, block new entries instead of trading blind (0 = trade normally when the feed is down)
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
    min_hold_minutes: int = 0  # anti-churn: the RL "cut it early" discretionary exit can't fire until a trade is this old (0 = off). Safety exits (stop-loss, breakeven, take-profit, time stop) ALWAYS fire regardless. Session 48: out-of-sample test showed the raw agent churned 100-160 trades/day at a loss; a 30-min floor turned unseen-day P&L from -15.8% to +1.6% (the profit-maximizing point; 60 min over-held back to breakeven).
    orb_retest_required: bool = False  # MambaFX-style: don't chase an extended breakout candle early in the session, wait for a retest
    high_conviction_adx: float = 0.0  # 0 = off; ADX above this bypasses the daily trade cap (still has to pass every other filter)
    max_high_conviction_overrides: int = 2  # hard cap on how many cap-bypass trades can happen in one day
    asian_session_budget_pct: float = 0.0  # 0 = off; e.g. 0.4: only 40% of max_trades_per_day may be spent during the Asian session, saving the rest for London/NY
    weekend_crypto_caution: bool = True  # halve risk sizing on crypto trades during the weekend forex-closed window (documented thinner liquidity, session 19)
    loss_budget_pct: float = 0.8  # only ever risk this fraction of the daily/max loss limits; the rest is slippage insurance (stops are not guaranteed fills)
    rollover_blackout: bool = False  # no new non-crypto entries around the 5pm ET rollover (spreads spike, futures venues pause 5-6pm ET)
    friday_flatten: bool = False  # close all non-crypto positions in the last half hour before Friday 5pm ET (prop rule: no weekend holding without an add-on)
    symbol_cooldown_minutes: int = 0  # after a losing close on a symbol, wait this long before re-entering IT (0 = off; anti-revenge-trading)
    trail_after_target: bool = False  # hybrid exit: at the fixed target, convert to an ATR trailing stop (floor +1R) instead of cashing out -- OFF until session 34's exit fix proves itself on real trades
    vol_spike_entry_filter: float = 0.0  # 0 = off; e.g. 3.0: skip NEW entries when the last completed bar's range exceeds this multiple of ATR14 (don't chase a flash move into wide spreads)
    adr_exhaustion_pct: float = 0.0  # 0 = off; e.g. 1.0: skip NEW entries once today's range has consumed this fraction of the 14-day average daily range (late entries into a spent day have poor odds)
    zone_min_touches: int = 0  # 0 = off; e.g. 2: skip entries at a nearby swing level that hasn't been tested at least this many times in real history (fresh/virgin levels reverse ~70% of the time in backtests -- the desk shouldn't trust a "breakout" through one)
    max_live_spread_multiple: float = 0.0  # 0 = off; e.g. 3.0: skip entries when the broker's LIVE bid/ask spread is this many times the symbol's normal spread (measured, not guessed)
    symbol_probation: bool = False  # half-size any symbol whose own closed-trade record is net-negative over a real sample (journal-driven, self-updating)
    symbol_probation_min_trades: int = 10  # sample size before probation can trigger (below this, no judgment)
    challenge_target_pct: float = 0.0  # 0 = off; e.g. 0.10: once cumulative gain from the challenge's starting equity hits this, lock in the pass -- no new entries, ever (until the state file is cleared for a new challenge)
    weekend_trading_allowed: bool = True  # False for firms that ban ALL weekend trading outright: makes autopilot's weekend-crypto-fallback a no-op for this account even if --weekend-symbols is passed (by default or by habit), so a forgotten CLI flag can't violate the firm's rule
    max_leverage: float = 1.0  # total notional exposure cap as a multiple of equity; 1.0 = cash account (no leverage). Funded preset uses 5x -- see session 47: without leverage a 1m-scalp ATR stop (~0.5%) physically cannot risk more than ~0.1% of equity, no matter what risk_per_trade_pct says


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
        # Law, not a suggestion (user directive): 1-minute candles, matching
        # MambaFX's own documented timeframe -- previously this only held
        # because every launch script happened to pass --timeframe 1m;
        # now it's the actual default so nothing can launch a funded
        # account on anything else by accident.
        timeframe="1m",
        stop_loss_pct=0.015,
        take_profit_pct=0.03,
        max_trades_per_day=10,
        max_consecutive_losses=2,
        atr_stops=True,
        max_per_correlation_group=2,
        news_blackout=True,
        # A funded account forfeits on a single trade inside a red-news
        # window (newsguard.py). If the ForexFactory feed is unreachable and
        # we can't confirm the calendar is current, sitting out is cheap and
        # a blind trade is not -- so funded mode fails CLOSED when the guard
        # can't verify freshness, unlike paper mode which trades normally.
        news_fail_closed=True,
        breakeven_at_1r=True,
        session_aware_forex=True,
        htf_confirm=True,
        # Time stop (session 16): a scalp thesis on 1m/5m candles is stale
        # long before 2 hours -- if the trade hasn't even reached +1R by
        # then, the setup didn't confirm; free the capital and risk budget.
        max_hold_minutes=120,
        # Session 48 anti-churn floor (out-of-sample evidence): the RL
        # discretionary loser-cut can't fire until a trade is 30 min old.
        # Unseen-day P&L: churn (0 min) -15.8%, 15 min -3.6%, 30 min +1.6%
        # (best), 60 min +0.0%. 30 is the profit-maximizing point -- see the
        # min_hold_minutes field comment and scripts/minhold_pnl_check.py.
        min_hold_minutes=30,
        # MambaFX's own strategy is explicitly a breakout style; his
        # documented risk practice is retest-based, not chase-based (session
        # 17). Real ORB backtests show 65.9% of raw breakouts hit their
        # stop -- this is the fix.
        orb_retest_required=True,
        # Session 42: real backtest evidence (not folklore) says a fresh,
        # never-tested level reverses ~70% of the time, while a level
        # tested 4+ times breaks through ~75% of the time -- require at
        # least 2 prior touches (more than just the bar that formed it)
        # before trusting a breakout through the nearest zone.
        zone_min_touches=2,
        # Session 21: don't let a hard count cap block a genuinely
        # exceptional setup. ADX 20 is "trending, not choppy" (the normal
        # entry floor); 40+ is Wilder's "very strong trend" tier -- a real
        # step up, not just barely clearing the bar. Still has to pass
        # every other filter (RL signal, HTF confirm, ORB retest, etc.);
        # this only lifts the daily *count* limit, nothing else, and only
        # up to 2 extra trades a day so it can't become an unlimited hole
        # in the discipline.
        high_conviction_adx=40.0,
        max_high_conviction_overrides=2,
        # Session 22: the journal showed the desk spending its entire daily
        # trade budget in the overnight Asian session -- the thinnest
        # window -- leaving nothing for the London/NY overlap its own
        # research rates highest. Reserve most of the budget for the good
        # hours: at most 40% of max_trades_per_day may be spent while the
        # Asian session is the live one.
        asian_session_budget_pct=0.4,
        # Session 31: ~79% of funded-account failures are daily-drawdown
        # breaches, and the 5pm ET rollover is a documented spread-spike /
        # liquidity-vacuum window (futures venues literally pause 5-6pm
        # ET). Entries are blocked around rollover, and per-trade risk is
        # hard-capped to the REMAINING slice of 80% of each loss limit so
        # no single stop-out (even with slippage) should cross the line.
        rollover_blackout=True,
        # Clarity Traders (and many firms): no weekend holding without a
        # paid add-on -- everything non-crypto closes before Friday 5pm ET.
        friday_flatten=True,
        # Session 33: token-free self-correction inside the trading loop.
        # 30-minute re-entry cooldown after a loss on that symbol (the
        # same signal one cycle after a stop-out is revenge trading with
        # extra steps), and journal-driven per-symbol probation: a symbol
        # that is net-negative over >=10 of OUR OWN closed trades trades
        # at half size until its record recovers. Both self-update from
        # the journal on every close -- no human, no LLM, no tokens.
        symbol_cooldown_minutes=30,
        symbol_probation=True,
        # Session 40: don't enter right after a bar 3x its own ATR --
        # documented weekend-crypto 2-3x volatility spikes and flash
        # moves are the widest-spread moments to be buying into.
        vol_spike_entry_filter=3.0,
        # Session 41: don't buy late into a day that has already moved its
        # whole average range (90-100%% ADR consumed = continuation odds
        # drop, practitioner-documented), and never enter into a live
        # spread 3x its normal width -- measured from the broker's actual
        # bid/ask at entry time, not inferred from the clock.
        adr_exhaustion_pct=1.0,
        max_live_spread_multiple=3.0,
        # Session 47: without leverage the whole risk model was fiction --
        # a 1m-scalp ATR stop (~0.5%) needs ~3x equity in notional to risk
        # 1.5%, but notional was capped at 15% of equity and broker cash,
        # so realized risk per trade was ~$1-5 on a $5k account instead of
        # the configured $75 (live journal: avg loss $1.17 over 63 trades).
        # Real prop firms fund at 1:100 (FTMO standard) / 1:30 (swing,
        # Clarity); the desk uses a deliberately conservative 5x total
        # exposure cap and 3x per position -- just enough for the intended
        # risk to apply at the tightest ATR stops. Dollar risk per trade is
        # STILL bounded by risk_per_trade_pct + the loss-budget headroom
        # cap; leverage changes notional, not the stop-based risk math.
        max_leverage=5.0,
        max_position_pct=3.0,
    )
    base.update(overrides)
    return DeskConfig(**base)


def clarity_one_step_challenge_config(funded: bool = False, **overrides) -> "DeskConfig":
    """Preset for Clarity Traders' $5K One-Step TradeLocker challenge
    (confirmed session 46 -- same firm as the "Instant" tier researched in
    session 31, different tier/numbers): 10% target / 4% daily loss / 6%
    max loss during the challenge phase, 4% daily / 10% max once funded, no
    weekend trading, 1:30 leverage. Built on top of funded_account_config()
    so all the other guards (ATR stops, news blackout, session-aware forex,
    etc.) still apply.

    Session 31's fetched Clarity FAQ still governs the parts this tier's
    screenshot didn't show: EA's Allowed add-on is mandatory for the bot to
    be compliant at all (purchase it), and news trading is PERMITTED at
    this firm (not required to avoid it) -- the news_blackout guard here is
    risk discipline, not a compliance requirement.

    `funded=False` (default) is the evaluation phase: challenge_target_pct
    locks in the pass at +10% and stops taking new risk. `funded=True` is
    the live account after passing: no target lock (nothing to "pass"
    anymore), but the daily/max drawdown numbers switch to the looser
    live-account limits from the same screenshot.

    NOTE: verify these numbers against the actual firm's current rules page
    before connecting real money -- prop firms change terms, and this was
    read off a single screenshot, not a live API.
    """
    base = dict(
        max_daily_loss_pct=0.04,
        max_total_drawdown_pct=0.10 if funded else 0.06,
        challenge_target_pct=0.0 if funded else 0.10,
        # this firm never allows weekend trading at all (not just no
        # holding -- no NEW entries either, on anything, crypto included).
        # weekend_trading_allowed=False makes that code-enforced: autopilot
        # won't use the crypto weekend-fallback for this account even if
        # --weekend-symbols is passed by default or by habit.
        friday_flatten=True,
        weekend_trading_allowed=False,
    )
    cfg = funded_account_config(**base)
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def aquafunded_instant_config(**overrides) -> "DeskConfig":
    """Preset for AquaFunded's Instant Funded account (session 48 -- user's
    checkout screenshot + their own site/help-center, read directly, not
    secondhand): 3% max daily loss, 6% max total drawdown, 1:50 account
    leverage, TradeLocker platform, payout on demand (no 14-day cadence
    like Clarity), news trading allowed. No challenge phase -- Instant
    skips straight to a live account, so unlike
    clarity_one_step_challenge_config() there is no target to lock in;
    challenge_target_pct stays 0 (off) permanently for this preset.

    EA/bot policy -- quoted directly from AquaFunded's own help center
    (https://help.aquafunded.com, "Are EAs & Trade Copiers allowed?"):
    "Yes, EAs are allowed, provided they are used as part of your own
    personal trading strategy." This desk qualifies: it's a custom
    strategy, not a mass-market commercial EA, and does not do
    high-frequency trading or latency arbitrage (both explicitly
    prohibited) -- it checks in on a 1-minute cadence via GitHub Actions,
    not sub-second reaction to price ticks.

    The 1:50 leverage figure is the BROKER's ceiling, not this desk's own
    setting -- max_leverage stays at funded_account_config()'s
    conservative 5.0 (well under 1:50) unless explicitly overridden;
    raising it toward 1:50 would need the same evidence-based case any
    other risk change needs (see CLAUDE.md's evidence law), not just
    "the broker allows more."

    NOTE: verify these numbers against AquaFunded's live checkout/rules
    page before connecting real money -- this was read off a screenshot
    and their help-center article, not a live API, and firms change
    terms.

    UNVERIFIED (honesty flag, session 48): unlike Clarity (confirmed
    weekend trading banned outright), no explicit weekend-trading policy
    was found in what's been read of AquaFunded's rules/ToS. This preset
    leaves `weekend_trading_allowed` at the funded default (True) --
    that is an ASSUMPTION, not a confirmed fact. Verify directly before
    this account is ever live over a weekend.

    risk_per_trade_pct=0.0025 (session 48, evidence-law change): the
    funded default of 1.5%/trade means 4 consecutive full stop-outs
    breach this account's 6% max drawdown -- and the live paper journal's
    last 100 closed trades measured a 31% win rate (organic desk trading
    was ~flat once a one-off July-14 leverage-test burst is excluded), a
    rate at which 4+ losing streaks are routine. The risk-sweep Monte
    Carlo on real bootstrapped data agreed from the other side: 95% of
    simulated months busted at 0.5%/trade risk and 100% at 0.75% under
    these exact 3%/6% rules. Survival at a weak edge is bought with
    smaller per-trade risk, full stop: at 0.25% it takes 24 consecutive
    full stop-outs (with the daily 3% halt tripping every 12) to lose the
    account, which buys the nightly self-train weeks of runway to improve
    the edge instead of days. Raising this back up requires new journal
    evidence of a durable edge, per CLAUDE.md's evidence law.
    """
    base = dict(
        max_daily_loss_pct=0.03,
        max_total_drawdown_pct=0.06,
        challenge_target_pct=0.0,  # Instant: no challenge phase, no target to lock
        news_blackout=True,  # risk discipline, not required by this firm (news trading IS permitted)
        risk_per_trade_pct=0.0025,  # see docstring: survival-first sizing, evidence-law change
    )
    cfg = funded_account_config(**base)
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


# Correlated clusters: N positions inside one cluster are effectively ONE
# bet at N-times size ("hidden leverage"), because these names move together
# intraday. The desk caps entries per cluster.
CORRELATION_GROUPS = {
    # NAS100 grouped with tech (not "us-broad") to match NQ=F's existing
    # placement -- Nasdaq is heavily tech-weighted, same reasoning as
    # treating it as an extension of the big-tech cluster rather than a
    # separate broad-market bet.
    "us-tech": {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META",
                "TSLA", "AVGO", "AMD", "QQQ", "NQ=F", "NAS100"},
    # US30/US500/US2000 are this desk's OWN watchlist names (used in every
    # launch command) for Dow/S&P/Russell -- found missing here (session 46):
    # correlation_group() does exact string matching, so without these the
    # cap silently did nothing for 3 of the 4 US index CFDs this desk
    # actually trades, letting 4 simultaneous index positions (effectively
    # one 4x-concentrated "US market direction" bet) go completely uncapped.
    "us-broad": {"SPY", "DIA", "IWM", "ES=F", "YM=F", "RTY=F",
                 "US30", "US500", "US2000"},
    # silver rides gold's macro drivers (~0.8 daily correlation) -- two
    # metals positions are one doubled metals bet, so they share a cluster.
    # GOLD/SILVER are this desk's own watchlist names (same session-46 gap
    # as the indices above -- XAUUSD/XAGUSD are TradeLocker's broker-side
    # aliases, not what the desk's own symbol list actually uses).
    "gold": {"GC=F", "GLD", "IAU", "XAUUSD", "SI=F", "SLV", "XAGUSD",
             "GOLD", "SILVER"},
    "oil": {"CL=F", "USO", "XLE", "OIL"},
    # Any pair with USD as one leg moves together on a broad USD swing,
    # even with different signs -- that's still one concentrated bet on
    # "USD direction," the exact hidden-leverage pattern this cap exists
    # for (session 18: watchlist grew from 3 pairs to all 12 session-aware
    # pairs, so this needed to grow with it or the cap would silently stop
    # covering 9 of them).
    "usd-fx": {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCHF", "USDCAD",
               "EUR_USD", "GBP_USD", "USD_JPY"},
    # JPY-crosses without USD in them -- these move together on yen
    # risk-sentiment (carry-trade unwinds etc.) independent of USD-fx.
    "jpy-crosses": {"EURJPY", "GBPJPY", "AUDJPY"},
    "eur-crosses": {"EURGBP", "EURCHF"},
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


def currencies_for_symbol(symbol: str) -> set:
    """The currencies whose news moves this symbol. A 6-letter forex pair
    splits into its two legs (EURJPY -> {EUR, JPY}); everything else
    (indices, metals, oil, crypto) is USD-driven and already covered by
    the cycle-level USD news guard, so returns an empty set here."""
    s = symbol.upper().replace("_", "").replace("/", "")
    if len(s) == 6 and s.isalpha():
        return {s[:3], s[3:]}
    return set()


def zone_touch_count(df, level: float, tolerance_pct: float = 0.0015,
                      lookback_bars: int = 300) -> int:
    """How many distinct times price has approached `level` (within
    tolerance_pct) in the last lookback_bars, collapsing consecutive
    touching bars into one touch event.

    Session 42 research finding, the opposite of common trading folklore:
    fresh (0-touch) zones hold/reverse ~70% of the time; zones already
    tested 4+ times break through ~75% of the time. More touches makes a
    level WEAKER (more likely to finally give way), not stronger. This
    lets the desk tell a virgin level (unreliable, likely fakeout if
    'broken') from a seasoned one (real continuation odds behind it)."""
    try:
        cols = {str(c).lower(): c for c in df.columns}
        if "high" not in cols or "low" not in cols or level <= 0:
            return 0
        high = df[cols["high"]].tail(lookback_bars)
        low = df[cols["low"]].tail(lookback_bars)
        band = level * tolerance_pct
        touching = (high >= level - band) & (low <= level + band)
        if not touching.any():
            return 0
        # collapse consecutive touching bars into one touch event so a
        # single pause at the level doesn't get counted N times
        starts = touching & ~touching.shift(1, fill_value=False)
        return int(starts.sum())
    except Exception:
        return 0


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

# Every pair the session-awareness system knows about, regardless of
# which correlation cluster it happens to sit in -- used to detect "is
# this symbol a currency pair at all" independent of correlation grouping
# (those are two separate concerns; session 18 fixed a bug where they'd
# been conflated via a shared "usd-fx" string).
ALL_SESSION_FOREX_PAIRS = set().union(*FOREX_SESSION_PAIRS.values())


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


def is_weekend_forex_gap(now=None) -> bool:
    """True during the ~Friday 5pm ET to Sunday 5pm ET forex/index closure
    -- the window the weekend crypto fallback (autopilot.py) covers.
    Session 19 found weekend crypto liquidity has gotten measurably worse
    since spot ETFs concentrated institutional market-making into weekday
    hours (+11% trading costs, -9% depth vs weekdays); this flags that
    window so entries taken in it can be sized more cautiously instead of
    pretending it's an ordinary trading session."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = (now or datetime.now(tz=ZoneInfo("America/New_York"))).astimezone(
        ZoneInfo("America/New_York")
    )
    wd, hour = now.weekday(), now.hour
    if wd == 5:  # Saturday
        return True
    if wd == 4 and hour >= 17:  # Friday evening
        return True
    if wd == 6 and hour < 17:  # Sunday before reopen
        return True
    return False


def is_rollover_window(now=None) -> bool:
    """True around the 5pm ET daily rollover: banks pause pricing, spreads
    spike 5-20x for minutes (a 20-pip EURUSD spread is documented), and the
    CME futures venues behind US30/NAS100/US500/GOLD/OIL literally halt
    5-6pm ET. A stop can be TRIGGERED by the widened quote alone, so new
    entries here start life exposed to a phantom stop-out. Window: 4:45pm
    to 6:15pm ET, covering the pre-rollover liquidity drain and the
    post-reopen re-quote period."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = (now or datetime.now(tz=ZoneInfo("America/New_York"))).astimezone(
        ZoneInfo("America/New_York")
    )
    minutes = now.hour * 60 + now.minute
    return (16 * 60 + 45) <= minutes < (18 * 60 + 15)


def is_friday_close_window(now=None) -> bool:
    """True in the last half hour before the Friday 5pm ET weekly close.
    Prop firms commonly ban holding positions over the weekend unless a
    specific add-on was purchased (Clarity Traders: 'Without this add-on,
    all positions must be closed before market close on Friday') -- the
    desk uses this window to close everything down in time."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = (now or datetime.now(tz=ZoneInfo("America/New_York"))).astimezone(
        ZoneInfo("America/New_York")
    )
    if now.weekday() != 4:  # Friday only
        return False
    minutes = now.hour * 60 + now.minute
    return (16 * 60 + 30) <= minutes < (17 * 60)


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


def tradeability_score(symbol: str, df, now=None) -> float:
    """Composite 'how good is this instrument to trade RIGHT NOW' score,
    built from the three components professional day traders actually rank
    instruments by (session 25 research: volatility, liquidity, trend
    strength -- and the desk's own filters already measure each):

    - trend strength: ADX/25, capped at 2.0 (choppy < 0.8, strong > 1.2)
    - movement potential: ATR% scaled so ~0.15% per bar = 1.0, capped 2.0
      (a market that isn't moving can't pay for its own spread)
    - session liquidity: the forex session score (0-2) for currency pairs;
      non-forex (futures) get a neutral 1.0 since they trade near-24h

    Score is only used to ORDER candidates -- every hard filter (ADX
    floor, HTF confirm, session skip, correlation cap...) still applies
    unchanged afterward. Ranking decides who gets first shot at the open
    slots, not who's allowed in.
    """
    try:
        adx = adx_value(df)
        trend = min((adx or 0.0) / 25.0, 2.0)
        atr = atr_pct(df)
        movement = min((atr or 0.0) / 0.0015, 2.0)
        compact = symbol.upper().replace("_", "").replace("/", "")
        if compact in ALL_SESSION_FOREX_PAIRS:
            session = float(forex_session_score(symbol, now))
        else:
            session = 1.0
        return trend + movement + session
    except Exception:
        return 0.0


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
        challenge_target_guard: Optional[ChallengeTargetGuard] = None,
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
        self.challenge_target_guard = challenge_target_guard or (
            ChallengeTargetGuard(
                target_pct=self.config.challenge_target_pct,
                state_path=data_path(f"challenge_target_state_{self.broker.name}.json"),
            )
            if self.config.challenge_target_pct > 0
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

        # 2a-ter. Challenge target lock: once the evaluation's cumulative
        #    profit target is reached, stop taking new risk entirely -- a
        #    banked pass is worth more than the marginal upside of one more
        #    trade. Existing positions above were already managed/exited.
        if self.challenge_target_guard is not None:
            target_locked, target_msg = self.challenge_target_guard.check(self.broker.equity())
            report.notes.append(f"[risk] {target_msg}")
            if target_locked:
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

        # Payout radar: the desk can't press the withdraw button (that is
        # the owner's dashboard + wallet), but it knows its own journal --
        # the moment the account clears the payout gates, say so loudly.
        try:
            payout = self.journal.payout_readiness()
            if payout["eligible"]:
                report.notes.append(
                    f"[payout] READY TO CASH OUT: ${payout['profit']:.2f} realized "
                    f"across {payout['trading_days']} trading days, consistency OK -- "
                    "request the payout from the firm's dashboard"
                )
        except Exception:
            pass

        # 2a2. Friday close-out (prop rule): firms like Clarity Traders ban
        #      holding over the weekend without a paid add-on. Close every
        #      non-crypto position in the last half hour before the Friday
        #      5pm ET close and open nothing new until the week ends.
        if cfg.friday_flatten and is_friday_close_window():
            non_crypto = [
                s for s in positions if not s.upper().endswith(("-USD", "-USDT"))
            ]
            if non_crypto:
                report.notes.append(
                    "[risk] Friday close-out: closing all non-crypto positions "
                    "before the weekend (no-weekend-holding rule)"
                )
                flatten = self.flatten_all(
                    reason="Friday close-out: no weekend holding on this account",
                    symbols=non_crypto,
                )
                report.actions.extend(flatten.actions)
            else:
                report.notes.append(
                    "[risk] Friday close-out window: flat into the weekend, "
                    "no new entries until the close"
                )
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
            # Fail-closed (funded): "no news in window" is only trustworthy if
            # the calendar was actually verifiable. If the feed is down and we
            # can't confirm freshness, don't trade blind through a window that
            # could forfeit the account -- exits above already ran.
            if cfg.news_fail_closed and not self._news_guard.is_data_fresh():
                report.notes.append(
                    "[news] calendar unverifiable (feed unreachable) -- funded "
                    "fail-closed: no new entries until it can be confirmed"
                )
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
        cap_reason = "scalper discipline: daily trade cap reached"
        if (
            trades_left_today is not None
            and cfg.asian_session_budget_pct > 0
            and active_forex_session() == "asian"
        ):
            asian_cap = max(1, int(cfg.max_trades_per_day * cfg.asian_session_budget_pct))
            asian_left = asian_cap - self.journal.trades_opened_today()
            if asian_left < trades_left_today:
                trades_left_today = asian_left
                cap_reason = (
                    f"session budget: {asian_cap} of {cfg.max_trades_per_day} daily trades "
                    "allowed during the thin Asian session -- saving the rest for London/NY"
                )
        high_conviction_overrides_left = (
            max(0, cfg.max_high_conviction_overrides
                - self.journal.count_trades_with_tag_today("high-conviction-override"))
            if cfg.high_conviction_adx > 0
            else 0
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
                if forex_session_score(symbol) == 0
                and symbol.upper().replace("_", "").replace("/", "") in ALL_SESSION_FOREX_PAIRS
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

        # "What's the best thing to trade right now?" -- rank every
        # remaining candidate by the composite tradeability score (trend
        # strength + movement potential + session liquidity) so the
        # strongest market gets first claim on the open slots. One history
        # fetch per symbol per cycle, reused by _consider_entry below.
        history_cache: Dict[str, object] = {}
        if len(ordered_candidates) > 1:
            scores: Dict[str, float] = {}
            for s, _info in ordered_candidates:
                try:
                    history_cache[s] = self.history_fn(s)
                    scores[s] = tradeability_score(s, history_cache[s])
                except Exception:
                    scores[s] = 0.0
            # manual mirror calls keep first claim regardless of score --
            # a human's explicit call isn't outranked by the auto-ranker
            ordered_candidates.sort(
                key=lambda item: (0 if item[1].get("manual") else 1, -scores.get(item[0], 0.0))
            )
            if ordered_candidates:
                best = ordered_candidates[0][0]
                report.notes.append(
                    f"[rank] best to trade right now: {best} "
                    f"(score {scores.get(best, 0.0):.1f}); full ranking: "
                    + ", ".join(f"{s} {scores.get(s, 0.0):.1f}" for s, _ in ordered_candidates[:8])
                )

        for symbol, info in ordered_candidates:
            use_high_conviction = False
            if trades_left_today is not None and trades_left_today <= 0:
                if high_conviction_overrides_left > 0 and not info.get("manual"):
                    try:
                        adx = adx_value(self.history_fn(symbol))
                    except Exception:
                        adx = None
                    if adx is not None and adx >= cfg.high_conviction_adx:
                        use_high_conviction = True
                if not use_high_conviction:
                    report.actions.append(DeskAction("skip", symbol, cap_reason))
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
                high_conviction=use_high_conviction,
                df=history_cache.get(symbol),
            )
            report.actions.append(action)
            if action.action == "buy" and action.ok:
                open_slots -= 1
                if use_high_conviction:
                    high_conviction_overrides_left -= 1
                elif trades_left_today is not None:
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
        breakeven_armed = False
        if force_exit:
            reason = force_exit_reason
        elif record:
            change = price / record.entry_price - 1.0
            # Max favorable/adverse excursion (session 47): record how far
            # every trade actually ran for/against us, cycle by cycle. 26 of
            # the first 63 live trades died at the time stop below +1R and
            # only 2 ever reached the 2R target -- whether the target is
            # simply too far can only be answered from MFE data, not vibes.
            # Stored as tags so it survives into the closed-trade record.
            mfe, mae = change, change
            excursion_changed = True
            for tag in list(record.tags):
                if tag.startswith("mfe:"):
                    prior = float(tag.split(":", 1)[1])
                    excursion_changed = change > prior
                    mfe = max(mfe, prior)
                    record.tags.remove(tag)
                elif tag.startswith("mae:"):
                    prior = float(tag.split(":", 1)[1])
                    excursion_changed = excursion_changed or change < prior
                    mae = min(mae, prior)
                    record.tags.remove(tag)
            record.tags.append(f"mfe:{mfe:.6f}")
            record.tags.append(f"mae:{mae:.6f}")
            if excursion_changed:
                self.journal.save()
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
            elif change >= target_pct and cfg.trail_after_target:
                # Session 37 hybrid exit (evidence: ATR-trail beat fixed
                # trails, PF 1.6 vs 1.1, in comparative NQ backtests; the
                # video-study traders turn 2R days into 5-7R days exactly
                # here): once the fixed target is REACHED, don't cash out --
                # trail one stop-distance below the trade's high-water mark,
                # floored so the exit can never drop below +1R. Chop gives
                # back part of the last R; real trend days keep running.
                hwm = change
                for tag in list(record.tags):
                    if tag.startswith("hwm:"):
                        hwm = max(hwm, float(tag.split(":", 1)[1]))
                        record.tags.remove(tag)
                record.tags.append(f"hwm:{hwm:.6f}")
                self.journal.save()
                trail_floor = max(hwm - stop_pct, target_pct - stop_pct)
                if change <= trail_floor:
                    reason = (f"trailing stop hit ({change:+.1%} off a "
                              f"{hwm:+.1%} high-water mark, target was {target_pct:+.1%})")
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
        if reason is None and not breakeven_armed and (record is None or change < 0.0):
            # Session 34 forensics on ALL 22 real desk trades: ZERO ever
            # reached the take-profit. 10 were scratched by this RL exit --
            # 7 of them while slightly GREEN -- and 11 hit the time stop.
            # The 1:2 payoff engine never got to run once. So the RL exit
            # is now asymmetric, matching the design's intent: it may cut a
            # trade that is LOSING (bailing out of a bad thesis before the
            # full stop is still legitimate), but a green trade below +1R
            # is left alone to reach its target, stop, or time stop.
            # (Session 26 already protects breakeven-armed winners; this
            # closes the same hole for the pre-1R stretch.)
            #
            # Session 48 anti-churn floor: this discretionary "cut it early"
            # exit is exactly what over-traded -- an out-of-sample test showed
            # the raw agent flipping 100-160 trades/day and bleeding -15.8%,
            # while blocking this exit until a trade is >= min_hold_minutes old
            # turned unseen-day P&L to +1.6% at 30 min (the profit-max point).
            # The block ONLY delays this discretionary loser-cut; every hard
            # exit above (stop-loss, breakeven, take-profit, trailing, time
            # stop) already fired earlier in this function and is unaffected,
            # so risk is never widened -- a real stop still gets you out now.
            too_new = False
            if record is not None and cfg.min_hold_minutes > 0:
                from datetime import datetime, timezone

                try:
                    entry_dt = datetime.fromisoformat(record.entry_time)
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                    age_min = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60.0
                    too_new = age_min < cfg.min_hold_minutes
                except Exception:
                    too_new = False
            if not too_new:
                try:
                    df = self.history_fn(symbol)
                    if self.agent.signal(df, holding=True) == "sell":
                        reason = "quant desk: RL agent says exit (position red, cutting the loser early)"
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
            self._online_learn(closed)
        if result.ok and force_exit:
            from bots.copytrader import manual as manual_mod

            manual_mod.consume_signal(symbol, self.manual_signals_path)
        report.actions.append(
            DeskAction("sell", symbol, reason, quantity=quantity, ok=result.ok)
        )

    def _online_learn(self, closed_record) -> None:
        """The missing half of 'learns from mistakes automatically': the
        journal-based setup veto (should_avoid) already re-reads real trade
        history every cycle with no retraining step, but the Q-agent's own
        table was still frozen between explicit `train` runs -- live
        trading only ever called signal() (a lookup), never _update() (the
        actual learning step). This closes that gap: every real trade that
        closes feeds its outcome straight back into the Q-table, live, so
        the model itself keeps adjusting from real experience, not just
        the setup-level veto layer on top of it. Best-effort: any failure
        here must never break a trade close."""
        if closed_record.pnl_pct is None or "admin" in closed_record.tags:
            return
        setup = closed_record.setup or ""
        if ":" not in setup:
            return  # manual/mirror trades don't carry a recorded state
        state = setup.split(":", 1)[1]
        try:
            df = self.history_fn(closed_record.symbol)
            next_state = self.agent.current_state(df, holding=False)
            reward = closed_record.pnl_pct / 100.0
            self.agent._update(state, "buy", reward, next_state)
            self.agent.save()
        except Exception:
            pass

    def _consider_entry(
        self,
        symbol: str,
        reason: str,
        equity: float,
        setup_base: str = "copytrade",
        manual: bool = False,
        high_conviction: bool = False,
        df=None,
    ) -> DeskAction:
        cfg = self.config

        # Rollover blackout: never START a trade into the 5pm ET liquidity
        # vacuum (crypto trades 24/7 through it and is exempt). Exits and
        # server-side stops on existing positions keep working.
        if (
            cfg.rollover_blackout
            and not symbol.upper().endswith(("-USD", "-USDT"))
            and is_rollover_window()
        ):
            return DeskAction(
                "skip", symbol,
                "rollover blackout: 4:45-6:15pm ET -- spreads spike at the "
                "daily close and futures venues pause; a fresh entry here "
                "risks a phantom stop-out on the widened quote alone",
            )

        # Quant desk vote (needs history; a missing feed means no veto). Manual
        # mirror calls carry the caller's setup tag so the journal can grade
        # that human's real performance separately.
        setup_suffix = ""
        symbol_atr = None
        try:
            if df is None:
                df = self.history_fn(symbol)
            vote = self.agent.signal(df, holding=False)
            if not manual:
                setup_suffix = ":" + self.agent.current_state(df, holding=False)
            if vote == "sell" and not manual:
                return DeskAction("skip", symbol, "quant desk: RL agent is bearish here")
            if cfg.atr_stops:
                symbol_atr = atr_pct(df)
            if cfg.vol_spike_entry_filter > 0 and not manual:
                # Flash-move guard (session 40): weekend crypto moves run
                # 2-3x weekday volatility on thin books (Kaiko), and an
                # entry right after an outsized candle is a chase into the
                # widest spreads of the move -- on any market, any day.
                spike_atr = symbol_atr if symbol_atr is not None else atr_pct(df)
                try:
                    cols = {str(c).lower(): c for c in df.columns}
                    if spike_atr and "high" in cols and "low" in cols:
                        last = df.iloc[-1]
                        last_range = (
                            float(last[cols["high"]]) - float(last[cols["low"]])
                        ) / float(last[cols["close"]])
                        if last_range > cfg.vol_spike_entry_filter * spike_atr:
                            return DeskAction(
                                "skip", symbol,
                                f"flash-move guard: last bar ranged {last_range:.2%}, "
                                f">{cfg.vol_spike_entry_filter:.0f}x ATR ({spike_atr:.2%}) "
                                "-- not chasing a spike into wide spreads",
                            )
                except Exception:
                    pass
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
            if cfg.zone_min_touches > 0 and not manual:
                # Session 42: predict off the ZONE, backed by real backtest
                # stats, not folklore. Nearest recent swing level to price
                # is the "zone" being tested; a level with too few prior
                # touches is unconfirmed -- fresh zones reverse ~70% of the
                # time in backtests, so a "breakout" through one is more
                # likely a fakeout than a real continuation.
                cols = {str(c).lower(): c for c in df.columns}
                if "high" in cols and "low" in cols and len(df) >= 60:
                    recent_high = float(df[cols["high"]].tail(50).max())
                    recent_low = float(df[cols["low"]].tail(50).min())
                    last_price = float(df[cols["close"]].iloc[-1])
                    level = (
                        recent_high
                        if abs(last_price - recent_high) <= abs(last_price - recent_low)
                        else recent_low
                    )
                    touches = zone_touch_count(df, level)
                    if touches < cfg.zone_min_touches:
                        return DeskAction(
                            "skip", symbol,
                            f"zone filter: nearest level {level:.5g} only tested "
                            f"{touches}x (need {cfg.zone_min_touches}) -- fresh, "
                            "unconfirmed zones reverse ~70% of the time in "
                            "backtests, not a reliable breakout yet",
                        )
        except Exception:
            pass

        # Per-symbol news blackout (session 43): the cycle-level guard only
        # watches USD; but a pair like EURJPY gets torn apart by an ECB or
        # Bank-of-Japan decision just as badly. Block THIS symbol around
        # high-impact news for EITHER of its currencies, without freezing
        # unrelated pairs. Non-forex (indices/metals/oil) is USD-driven and
        # already covered by the cycle-level guard.
        if cfg.news_blackout and not manual:
            non_usd = currencies_for_symbol(symbol) - {"USD"}
            if non_usd:
                try:
                    if not hasattr(self, "_news_guard"):
                        from bots.newsguard import NewsGuard

                        self._news_guard = NewsGuard(currencies=cfg.news_currencies)
                    blocked, msg = self._news_guard.blackout(currencies=non_usd)
                    if blocked:
                        return DeskAction("skip", symbol, f"news blackout ({symbol}): {msg}")
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

        if cfg.adr_exhaustion_pct > 0 and not manual:
            # Session 41: 90-100% of the average daily range already
            # traveled means continuation odds have measurably dropped
            # (practitioner-documented ADR discipline) -- a long taken
            # here is buying the top of a spent day.
            try:
                cols = {str(c).lower(): c for c in df.columns}
                if ("high" in cols and "low" in cols
                        and hasattr(df.index, "normalize")):
                    by_day = df.groupby(df.index.normalize())
                    day_ranges = (
                        by_day[cols["high"]].max() - by_day[cols["low"]].min()
                    )
                    if len(day_ranges) >= 4:
                        today_range = float(day_ranges.iloc[-1])
                        adr = float(day_ranges.iloc[:-1].tail(14).mean())
                        if adr > 0:
                            used = today_range / adr
                            if used >= cfg.adr_exhaustion_pct:
                                return DeskAction(
                                    "skip", symbol,
                                    f"ADR exhaustion: today already ranged "
                                    f"{used:.0%} of the average daily range -- "
                                    "late entries into a spent day have poor odds",
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

        # Anti-revenge cooldown: this symbol just stopped us out -- the
        # conditions that killed the last trade are usually still there one
        # cycle later, and immediate re-entry is how one loss becomes three.
        if cfg.symbol_cooldown_minutes > 0 and not manual:
            since_loss = self.journal.minutes_since_last_loss(symbol)
            if since_loss is not None and since_loss < cfg.symbol_cooldown_minutes:
                return DeskAction(
                    "skip", symbol,
                    f"cooldown: lost on {symbol} {since_loss:.0f}min ago -- "
                    f"waiting {cfg.symbol_cooldown_minutes}min before re-entering "
                    "the same market (anti-revenge-trading)",
                )

        rating = self.llm_rating(symbol)
        if rating in ("Sell", "Underweight"):
            return DeskAction("skip", symbol, f"AI committee rated {rating}")

        try:
            price = self.broker.price(symbol)
        except Exception as exc:
            return DeskAction("skip", symbol, f"no price data: {exc}")

        if cfg.max_live_spread_multiple > 0 and not manual:
            # Session 41: measure the spread actually being quoted right
            # now instead of only inferring from the clock. A spread 3x its
            # normal width means the fill starts that much deeper in the
            # hole -- the exact mechanism behind news/rollover stop-outs.
            try:
                from bots.spreads import spread_pct as _normal_spread

                live = self.broker.live_spread_pct(symbol)
                normal = _normal_spread(symbol)
                if live and normal and live > cfg.max_live_spread_multiple * normal:
                    return DeskAction(
                        "skip", symbol,
                        f"spread veto: live spread {live:.3%} is "
                        f"{live / normal:.1f}x the normal {normal:.3%} -- "
                        "entries here start deep in the hole",
                    )
            except Exception:
                pass

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
        if high_conviction:
            sizing_note += " (high-conviction override: exceptionally strong trend, bypassed the daily trade cap)"
        if (
            cfg.weekend_crypto_caution
            and symbol.upper().endswith(("-USD", "-USDT"))
            and is_weekend_forex_gap()
        ):
            risk_pct /= 2.0
            sizing_note += (
                " (weekend crypto: half size -- documented thinner liquidity "
                "since spot ETFs concentrated market-making into weekday hours)"
            )
        if cfg.symbol_probation:
            stats = self.journal.symbol_stats(symbol)
            if stats["trades"] >= cfg.symbol_probation_min_trades and stats["pnl"] < 0:
                risk_pct /= 2.0
                sizing_note += (
                    f" (probation: {symbol} is {stats['pnl']:+.2f} over "
                    f"{stats['trades']} of our own closed trades -- half size "
                    "until its record recovers)"
                )
        # Loss-budget headroom (session 31): cap this trade's risk to what's
        # LEFT of 80% of each loss limit, so no single stop-out -- even one
        # that slips past the stop -- should be able to cross a funded
        # account's termination line. ~79% of funded-account failures are
        # daily-drawdown breaches, and they happen exactly here: a normal-
        # sized trade taken late in an already-red day.
        if cfg.loss_budget_pct > 0:
            headrooms = []
            if self.guard is not None:
                headrooms.append(
                    ("daily loss limit",
                     self.guard.loss_headroom_pct(equity, cfg.loss_budget_pct))
                )
            if self.max_drawdown_guard is not None:
                headrooms.append(
                    ("max drawdown limit",
                     self.max_drawdown_guard.loss_headroom_pct(equity, cfg.loss_budget_pct))
                )
            for limit_name, headroom in headrooms:
                if headroom <= 0.0:
                    return DeskAction(
                        "skip", symbol,
                        f"risk desk: today's loss budget is spent "
                        f"({cfg.loss_budget_pct:.0%} of the {limit_name} -- the "
                        "remainder is slippage insurance, not tradeable risk)",
                    )
                if risk_pct > headroom:
                    sizing_note += (
                        f" (headroom cap: risking {headroom:.2%} not {risk_pct:.2%} -- "
                        f"only that much of the {limit_name} budget is left today)"
                    )
                    risk_pct = headroom

        risk_budget = equity * risk_pct / max(stop_pct, 1e-6)
        if cfg.max_leverage > 1.0:
            # Margin account: buying power is what's left of the total
            # exposure cap (max_leverage x equity), not settled cash. The
            # dollar risk of the trade is unchanged -- it's still
            # risk_pct * equity if the stop fills -- leverage only lets the
            # notional get large enough for that risk to actually apply
            # when the stop is tight (session 47).
            exposure = 0.0
            for held_symbol, held_qty in self.broker.positions().items():
                try:
                    exposure += abs(held_qty) * self.broker.price(held_symbol)
                except Exception:
                    pass
            buying_power = max(equity * cfg.max_leverage - exposure, 0.0)
        else:
            buying_power = self.broker.cash()
        budget = min(risk_budget, equity * cfg.max_position_pct, buying_power)
        quantity = round(budget / price, 4) if price > 0 else 0.0
        if quantity <= 0:
            return DeskAction("skip", symbol, "risk desk: no budget available")

        result = self.broker.buy_bracket(symbol, quantity, stop_pct, take_profit_pct)
        if result.ok:
            opened = self.journal.open_trade(
                symbol, "long", result.quantity or quantity,
                result.fill_price or price, setup=setup, notes=reason,
                stop_pct=record_stop,
            )
            if high_conviction:
                opened.tags.append("high-conviction-override")
                self.journal.save()
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
