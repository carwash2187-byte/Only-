# Day-Trading Study Notes (bot training reference)

Compiled 2026-07-14 from public strategy documentation and backtested
evidence. These notes drive the intraday features implemented in
`bots/learning/agent.py` and the scalper discipline in `bots/organization.py`.

## What the evidence supports (and what the bot now uses)

1. **Opening Range Breakout (ORB)** — the first 15–30 minutes of the session
   set a high/low range; trading the break of that range is the most
   consistently documented intraday edge. Public backtests report ~56% win
   rate at ~1.8:1 reward/risk on the 15-minute range, degrading to ~40%
   without filters and improving toward ~55–65% with them.
   ([TradeAlgo](https://www.tradealgo.com/trading-guides/day-trading/opening-range-breakout-strategy-how-to-trade-the-first-30-minutes),
   [ChartingLens](https://chartinglens.com/blog/opening-range-breakout-strategy),
   [LiteFinance](https://www.litefinance.org/blog/for-beginners/trading-strategies/opening-range-breakout-strategy/))
2. **VWAP alignment** — longs are materially better when price is above the
   session VWAP, shorts below; stacking this filter on ORB sharply cuts
   false breakouts. VWAP-reversion in ranges shows 60–70% win rates at
   1.5–2:1. ([TradeZella](https://www.tradezella.com/blog/scalping-strategies),
   [CapMint](https://www.capmint.com/learn/articles/types-of-scalping-trading-strategies))
3. **Time-of-day matters** — the first hour concentrates volume/volatility
   (where breakout setups work); the dead middle of the session mean-reverts;
   the close brings volume back. Session position is a real feature.
4. **The math that keeps accounts alive**: small fixed risk per trade
   (0.5–1%), asymmetric reward:risk (≥1.5:1), a hard daily stop, and a trade
   cap per day. Expectancy can be positive even at a 40% win rate if the
   ratio holds; no win rate survives oversized losses.

These same techniques are what the credible open-source intraday bots
implement — e.g. [althk/zerobha](https://github.com/althk/zerobha) (ORB with
RSI/ADX/volume/VWAP-distance filters + a CPR/VWAP mean-reversion strategy)
and the VWAP-signal bots under the [vwap topic](https://github.com/topics/vwap?l=python).

## On MambaFX specifically

Publicly, MambaFX (Alex) teaches breakout scalping on NAS100/US30/forex with
explicit stop-loss/take-profit on every trade and heavy emphasis on
risk/reward management and psychology ([course listings](https://www.missionforex.com/index.php?route=product/product&product_id=2765),
[mambacourses.com](https://mambacourses.com/), [breakout strategy walkthrough](https://www.youtube.com/watch?v=hhuA-yyDdTw)).
That style maps almost exactly onto ORB + tight risk above — which is what
got encoded.

**Honesty note:** his results are self-reported and course sales are part of
his business; "only loses 5%" is not an audited statistic. The bot copies
the *method* (breakout + strict risk), and the mirror mode
(`python -m bots mirror`) grades his actual calls with real journal data if
you feed them in. Trust the journal, not the marketing.

## How this landed in the bot

- The RL agent's market state now includes, on intraday candles:
  **VWAP side** (above/below session VWAP), **opening-range position**
  (above the first-30-min high / below the low / inside), and **session
  phase** (open hour / midday / last hour) — on top of the existing
  trend + RSI features. Daily-candle states are unchanged.
- Day-trading mode defaults became scalper-shaped: 1.5% stop, 3% target
  (1:2 risk:reward), max 10 trades/day, flatten everything before the close.
- The daily 5% circuit breaker and 1%-risk-per-trade sizing already existed
  and match the funded-account rules discussed (3%/5% limits get configured
  per account when one is connected).

## Session 2 findings (bracket orders + honest testing)

- **Exchange-side stop losses**: polling stops every 5 minutes leaves gaps; the
  professional pattern is a [bracket order](https://docs.alpaca.markets/us/docs/orders-at-alpaca) —
  entry + stop-loss + take-profit submitted together, enforced by the broker
  the instant the entry fills, one leg auto-cancels the other. Gotchas from
  [Alpaca's docs](https://alpaca.markets/learn/placing-bracket-orders): whole
  shares only (no fractional brackets), penny-rounded prices, and in fast
  markets both legs can fill before cancellation. Implemented in
  `AlpacaBroker.buy_bracket`; the desk reconciles journal entries when a
  bracket leg fires between cycles.
- **Out-of-sample backtesting** (`python -m bots backtest`): train on 70% of
  history, test on the 30% the agent never saw — the same walk-forward
  principle behind freqtrade backtesting / vectorbt / backtesting.py. First
  honest results (5m bars, 1 month): SPY LOST to buy-hold by 1.8%, NVDA LOST
  by 12.0%, TSLA BEAT by 1.2%. **Conclusion: the edge is not proven yet.**
  In-sample training numbers flatter; this is the number that gates real
  money, and right now it says keep training.

## What a "trained enough" bot looks like

Judge on the journal, nothing else: 25+ closed trades, positive average
PnL per trade, losses clustered near -1R (proof stops are respected), no
circuit-breaker days. Paper results still overstate live results (no
slippage pressure) — size down at the transition.

## Session 3 findings (instruments, banks, funded-account survival)

**Gold (XAUUSD/GC):** trades ~200-500 pips daily range (vs ~80 for EURUSD).
London+NY overlap (8:00-12:00 ET) sets the daily high/low ~70% of the time
-- that's THE gold window. 8:30 ET on data days (NFP/CPI) is the single most
violent timestamp of the week: 1,000+ pip moves, spreads widen 20x.
([NordFX](https://nordfx.com/en/traders-guide/best-time-to-trade-gold-xauusd-sessions-volatility-news),
[ACY](https://acy.com/en/market-news/education/best-time-trade-gold-xauusd-sessions-news-091755/))

**Indices (NAS100/US30):** NAS100 is the prop-firm favorite -- 1.5-2.0%
daily range (US30: 0.8-1.2%), tech-heavy so it swings hard on Fed rates
and tech earnings. Tightest spreads during US cash hours 9:30-16:00 ET.
([BrightFunded](https://brightfunded.com/blog/mastering-the-nas100-a-strategic-guide-for-prop-firm-traders))

**"Smart money" / bank concepts (order blocks, liquidity sweeps):** the
honest read -- no rigorous public evidence these describe what banks
actually do; even practitioner guides admit the value is a *consistent
decision framework*, not proven institutional mechanics. The bot keeps
what's measurable from the same family (VWAP = the real institutional
benchmark, opening range) and skips the narrative parts.

**Funded-account survival rules (beyond the loss limits):**
- Consistency caps: best day must stay under ~35-50% of total profit --
  grind small daily, no hero trades
- News blackouts: many firms hard-prohibit positions +/-10min around
  red-folder news; violation forfeits the account EVEN IF the trade wins
- Weekend holding bans on several firms -> the flatten-before-close habit
  already matches this
([FundedNext](https://fundednext.com/blog/prop-firm-trading-rules),
[FundingPips](https://help.fundingpips.com/hc/en-us/articles/34504137479441-News-Trading-Weekend-Holding))

**Position-sizing math (risk of ruin):** with positive expectancy and 1%
risk per trade, risk of ruin stays under 0.5%; full-Kelly sizing produces
40-60% drawdowns (account-killers). Half/quarter-Kelly keeps ~75% of the
growth at a fraction of the pain. The bot's 1% fixed-fractional sizing is
the textbook-correct choice. ([CrossTrade](https://crosstrade.io/learn/risk-management/risk-of-ruin))

**Applied to the bot this session:**
- bots/newsguard.py: live economic calendar (ForexFactory weekly JSON),
  desk refuses new entries +/-10min around high-impact USD news (exits
  still run). Fail-safe: feed down -> trade normally, never lock up.
- Trained on gold (GC=F), NASDAQ (NQ=F), Dow (YM=F), GBPUSD 5m bars.
- Fixed YouTube channel resolution (externalId) -- education channels
  now watchable via python -m bots watch.

## Session 4 findings (trade management + hidden leverage)

**Break-even stops:** standard professional practice -- once a trade reaches
+1R (one stop-distance in profit), move the stop to entry: worst case
becomes "out flat" instead of a loss. Known tradeoff: normal pullbacks
sometimes stop out trades that would have worked; research on trailing
stops still shows positive excess returns even after transaction costs.
([Trading Heroes](https://www.tradingheroes.com/move-stoploss-breakeven/),
[PM Research](https://www.pm-research.com/content/iijindinv/14/1/29))
Implemented: `breakeven_at_1r` (default on) in the desk.

**The correlation trap ("hidden leverage"):** five 1%-risk positions in
correlated names is not 5 bets, it's ONE 5% bet -- intraday correlations
run especially hot because everyone reacts to the same news at once. The
bot's own day-1 basket (AAPL/MSFT/NVDA/AMZN/GOOGL) was exactly this
mistake. ([TradingPub](https://thetradingpub.com/kane-shieh/the-hidden-correlation-trap-in-multi-stock-day-trading/),
[TradeThatSwing](https://tradethatswing.com/how-do-i-size-positions-when-trading-multiple-correlated-assets/))
Implemented: correlation clusters (us-tech, us-broad, gold, oil, usd-fx)
with `max_per_correlation_group=2` (default on).

**Transcript access note:** YouTube blocks caption fetches from datacenter
IPs -- education-channel discovery works from this environment (titles/RSS),
full transcripts need a residential connection.

## Session 5 findings (volatility-adaptive risk + psychology guards)

**ATR (volatility-adjusted) stops/sizing:** fixed % stops treat gold and SPY
identically -- wrong: gold's daily range is ~5x wider. Standard practice is
stop = 1.5x ATR(14) for day trades, with position size = dollar risk / stop
distance, so volatile instruments automatically get wider stops AND smaller
size (same dollars at risk everywhere).
([LuxAlgo](https://www.luxalgo.com/blog/how-to-use-atr-for-volatility-based-stop-losses/),
[QuantifiedStrategies](https://www.quantifiedstrategies.com/volatility-based-position-sizing/))
Implemented: `atr_stops` (on in day-trading mode), per-trade stop stored on
the journal record, 1:2 R:R shape preserved, clamped 0.3%-5%.

**Revenge trading / the 2-loss rule:** after consecutive losses, judgment is
measurably impaired and the urge to "win it back" produces oversized,
low-quality trades. Standard pro rule: 2 straight losses = done for the day.
([TradeZella](https://www.tradezella.com/blog/revenge-trading),
[CrossTrade](https://crosstrade.io/learn/trading-psychology/revenge-trading))
Implemented: `max_consecutive_losses` (2 in day-trading mode, 3 default);
the desk stops opening trades for the day once the streak hits the cap --
a winner resets it. A bot doesn't feel revenge, but a model having a bad
regime day produces the same loss streak; stopping is right either way.

**Bug found during implementation (worth recording):** the agent's feature
cache stored a DataFrame inside df.attrs; pandas compares attrs dicts during
any later pd.concat and a DataFrame there raises "truth value is ambiguous".
Fixed with a plain holder object + concat-free ATR. Lesson: hidden state on
shared data structures bites later, far from where it was planted.

## Session 6 findings (ending the day right + regime awareness)

**Daily profit target ("quit while ahead"):** prop traders set a modest
daily target (1-2%) and STOP once hit -- the fastest way to fail a funded
account after a good morning is giving it back in the afternoon, and
consistency rules punish oversized single days anyway.
([ElTraderFinanciado](https://www.eltraderfinanciado.com/en/blog/profit-target-prop-firms),
[MasterFunders](https://masterfunders.com/prop-firm-rules/))
Implemented: `daily_profit_target_pct` (2% in funded mode) -- once today's
gain hits target, the desk cashes out every position and refuses new
entries until tomorrow.

**Regime detection (when NOT to trade):** ADX(14) below ~20 marks a choppy
market where breakout entries mostly fail; the credible open-source bots
(e.g. [CeyxTrading/Breakout-Bot](https://github.com/CeyxTrading/Breakout-Bot),
[QuantTradingOS/Market-Regime-Agent](https://github.com/QuantTradingOS/Market-Regime-Agent))
gate breakout entries on trend strength. Implemented: `min_adx` (20 in
funded mode) -- entries skipped in chop, manual mirror calls exempt.
Next-level options noted for later: HMM-based regime models
([Sakeeb91/market-regime-detection](https://github.com/Sakeeb91/market-regime-detection)).

## Session 7 findings (autopsy of tonight's real losses + MambaFX's actual style)

**Root cause of tonight's 4-loss streak, found by reading the actual
journal entries, not more theory:** 3 of 4 losses shared the exact same
"opening range breakout, last hour of session" signal -- on crypto, which
trades 24/7 and has no real session open or close. That feature was
computing on an arbitrary UTC-midnight boundary with zero trading
meaning. Fixed: `_is_continuous_market()` detects 24/7 assets (bars/day
>=90% of a full day) and drops opening-range/session-phase for them,
keeping VWAP (still valid -- institutional systems reset crypto VWAP
daily too). Stock/forex sessions are unaffected.

**MambaFX, verified against his actual channel (@mambafx) this time:** he
scalps **1-minute** charts on NAS100/US30, more aggressive than the 5m
default built earlier. Confirmed the bot can run 1m (`--timeframe 1m`,
verified live fetch works). Also worth flagging honestly: his channel's
recent uploads skew toward course/monetization content ("You WON'T
believe how much this costs") over pure technical breakdowns -- same
"trust the journal, not the marketing" rule from session 1 applies.

**Signal repaint, found while reviewing other scalping bots
([nyao_scalper_mt5](https://github.com/elrizwiraswara/nyao_scalper_mt5)
explicitly calls this out as "new-bar entry evaluation that removes
intrabar signal repaint"):** a live bot deciding off its most recent
fetched candle is often deciding off a candle that hasn't finished
forming yet -- verified live, a "5-minute" bar was 5 seconds old. The
same instant can produce a different signal once that bar actually
closes. Fixed: `_drop_forming_bar()` trims an in-progress bar before any
live signal/state call; daily+ bars (backtesting, training) are
unaffected since those are always fully historical already.

**Dead-market filter cross-reference:** the same MT5 scalper repo's "dead
market filter when volatility is too low" is the same concept as the
ADX regime filter already built in session 6 -- independent confirmation
this is a real, recognized pattern, not a one-off idea.

## Session 8 findings (crypto vs. forex -- settled with evidence, not guesswork)

Asked directly: is crypto actually the right market for this bot? Researched
it properly instead of assuming.

**Cost:** crypto CFD spreads run 3-5x wider than major forex pairs (wider
underlying-market fragmentation + higher volatility risk premium) --
concretely, one comparison found a $12 BTC/USD spread vs forex majors
typically under $1-2 equivalent; at 10 trades/day that gap alone can run
$50k+/year at scale. That directly eats into a 1.5% ATR stop.
([ForexSpreadCompare](https://forexspreadcompare.com/artigos/crypto-spread-comparison))

**Structure:** confirmed in session 7 -- opening-range/session-phase
features have no real meaning on a 24/7 market. Forex has genuine session
structure the strategy is built around.

**Where the actual edge concentrates:** the London/NY overlap (~8am-12pm
ET) carries the highest share of real institutional forex volume
(London alone ~35-40% of global FX turnover) -- directly matches the
ORB+VWAP strategy already built, on a market where "opening range" means
something. ([Equiti](https://www.equiti.com/sc-en/news/trading-ideas/london-session-why-the-forex-market-becomes-most-active-in-european-trading/))
(Filtered out several "secret institutional bank levels" articles from
one recurring author -- same unverifiable-marketing pattern flagged in
session 1; kept only the measurable volume/timing claims.)

**MambaFX cross-check:** his own site (mambafx.co) also advertises a
managed-funds service alongside the education -- a different, higher-risk
offering than a course, with the same "no independent verification"
caveat as before.

**Decision: switched the paper session from crypto to forex majors**
(EURUSD/GBPUSD/USDJPY), still free (Yahoo data, no broker account),
still 1-minute charts at MambaFX's pace. Crypto remains available as the
after-hours fallback when literally nothing else is open, but it is no
longer the default -- it was the wrong instrument for this strategy, not
just an inconvenient one.

## Session 9 findings (which specific instruments, not just which asset class)

Session 8 settled asset class (forex majors over crypto). This round asked
the next-level question professional day traders actually answer every
morning: of the pairs on the watchlist, which ones are worth trading *right
now*, today?

**Stock-screening side (for reference; the bot doesn't currently trade
individual stocks intraday, so not implemented, but recorded for when it
does):** the standard professional day-trading screener stack is price
range, relative volume (RVOL) above ~2.0 (confirms a move isn't just noise
-- institutional/news-driven), float under ~20M shares for the biggest
percentage movers, percentage change (gap%), market cap, and ATR as a
minimum-movement filter. Pre-market scans (before 9:30am ET) look for
overnight news and gap-ups. ([Warrior Trading-style screener criteria via
search results])

**Forex side (what actually applies to this bot right now):** a currency
pair is only genuinely liquid while one of its two home markets is open --
trading it outside that window means wider spreads and choppier,
non-institutional price action. Confirmed this is exactly the kind of
mistake traced in session 7 (a signal taken in effectively a dead market).
Real desks pick pairs by session:
- **London/NY overlap (8am-12pm ET, highest liquidity of the whole day):**
  EURUSD, GBPUSD, USDCHF, USDCAD
- **London (3am-8am ET):** EURUSD, GBPUSD, EURJPY, EURGBP, EURCHF, GBPJPY
- **New York (12pm-5pm ET):** EURUSD, USDJPY, USDCHF, USDCAD, AUDUSD, NZDUSD
- **Asian (rest of the day):** USDJPY, AUDUSD, NZDUSD, AUDJPY, EURJPY, GBPJPY
([OANDA](https://www.oanda.com/us-en/trade-tap-blog/trading-knowledge/when-is-the-best-time-for-forex-trading/),
[Babypips](https://www.babypips.com/learn/forex/session-overlaps))

**Implemented:** `bots/organization.py` gained `active_forex_session(now)`
(returns which of the four sessions is live by ET hour) and
`forex_session_score(symbol, now)` (2 = core overlap pair right now, 1 =
active in whichever single session is live, 0 = not actively traded right
now). New `DeskConfig.session_aware_forex` flag (on by default in
`funded_account_config()`, matching the "always trade like a funded
account" standing rule): when set, `run_once()` skips FX candidates
scoring 0 for the current session (logged as a "session filter" skip, same
transparency as every other risk-desk veto) and orders the remaining
candidates by score so the most-liquid-right-now pair gets first pick of
the open slots. Non-FX symbols are unaffected. Added
`test_forex_session_score_overlap_is_highest` and
`test_session_aware_forex_skips_off_session_pairs` to
`tests/test_bots.py` (48 tests passing).

## Session 10 findings (higher-timeframe confirmation, chasing consistency toward a daily target)

Asked for $100/day minimum. On the $5,000 practice size that's 2% a day --
which is exactly what `daily_profit_target_pct` in `funded_account_config()`
already locks in and cashes out at once hit. So the target mechanism
already existed; the honest gap is *consistency* -- actually winning
enough, often enough, to reach it regularly. No bot guarantees a fixed
dollar amount every single day; the lever that's actually researchable and
implementable is raising win rate/expectancy on the entries it does take.

**Researched:** multi-timeframe confirmation -- taking the entry signal on
a fast timeframe (this bot scalps 1m/5m) but only acting on it if a slower,
bigger-picture timeframe agrees with the direction. Cited results: signals
aligned across at least two timeframes ran ~58% win rate vs. ~39% for
non-aligned trades; higher-timeframe-filtered setups commonly move from
~50% win rate standalone to ~65-70% filtered. Standard spacing is a 4:1-5:1
ratio between entry and confirmation timeframe (e.g. 5m entries confirmed
on 1h, 15m entries confirmed on 1h-4h).
([Signal Pilot](https://blog.signalpilot.io/articles/multi-timeframe-confirmation/),
[Mind Math Money](https://www.mindmathmoney.com/articles/multi-timeframe-analysis-trading-strategy-the-complete-guide-to-trading-multiple-timeframes))

**Implemented:** `bots/organization.py` gained `trend_direction(df)` (the
same fast/slow SMA-cross rule the Q-agent's own "trend" feature already
uses, reused here so the filter agrees methodologically with what the
agent trained on), an `HTF_MAP` (1m->15m, 5m->1h, 15m->1h, 30m->4h), and a
new `DeskConfig.htf_confirm` flag (on by default in
`funded_account_config()`). When set, `_consider_entry()` fetches the
mapped higher timeframe and skips the buy if that bigger-picture trend is
down -- logged the same transparent way as every other risk-desk veto
("higher-timeframe filter: 1h trend is down"). Manual mirror calls (a
human's actual trade) are exempt, same as the existing ADX/quant-desk
vetoes -- this filter only applies to the bot's own automated entries.
`TradingDesk` gained an injectable `htf_history_fn` (separate from the
existing `history_fn`, since it needs a different timeframe) so this is
fully testable offline. Added
`test_htf_confirm_blocks_entry_against_higher_timeframe_downtrend` and
`test_htf_confirm_allows_entry_with_higher_timeframe_uptrend` to
`tests/test_bots.py` (50 tests passing).

**Autopsy of today's loss-streak trigger (4 straight losses -> desk
paused):** all 4 were crypto positions (ETH/BTC/SOL) opened before session
8's switch to forex, finally stopping out today -- not new losses from the
current forex desk, which hasn't taken a single trade yet today. The
pause is real and correctly triggered by the rule as written (it doesn't
distinguish "old position, new close"), but it's tail-end cleanup, not a
live forex trading problem.

## Session 11 (weekend market rotation + a bounded risk bump toward the $100/day target)

Two direct asks: trade different instruments depending on the day (forex
weekdays already existed; the actual gap is the ~48-hour weekend forex
closure where the bot was doing nothing), and take a bit more risk since
the hard 5% max-drawdown ceiling already exists.

**Weekend gap, quantified:** forex is closed roughly Friday 5pm ET to
Sunday 5pm ET -- about 2 of every 7 days the bot was simply idle,
sleeping in a loop with zero shot at the daily target on those days.
Crypto is the one market that's actually open through that window
(confirmed already in session 8 as the intentional after-hours fallback,
just never wired into the automatic scheduler).

**Implemented:** `bots/autopilot.py`'s `run_autopilot()` gained a
`weekend_symbols` parameter. Each cycle it checks: forex closed right now,
crypto open right now, and a weekend watchlist configured -> if so, run
that cycle against the crypto watchlist instead of sleeping through it.
Weekdays are completely unaffected (forex trades as before). Wired into
the CLI as `--weekend-symbols` (defaults to `BTC-USD,ETH-USD` automatically
when `--market forex --funded` are both set, so the funded desk gets this
for free). Added `test_autopilot_weekend_crypto_fallback`.

**Risk bump -- what actually changed and why it's still bounded:** the
real ceiling on a bad day was never the per-trade size, it's the 3% daily
/ 5% total drawdown circuit breakers, which trip on total account
movement regardless of how any individual trade was sized. Raising
`risk_per_trade_pct` from the generic 1% default to 1.5% in
`funded_account_config()` only changes how much a *winning* trade
contributes toward the 2% daily target (faster path to $100/day on a good
day) -- it does not raise, remove, or get anywhere near the 3%/5% hard
stops, which are unchanged and still the actual backstop. This is a small,
bounded change, not a loosening of the funded-account discipline.

51 tests passing after this session.

## Session 12 (automatic mistake-correction, no user action needed)

Asked specifically for the bot to auto-improve from mistakes without being
told, "like a real trader at super speed." The journal-based setup-veto
(`should_avoid()`) and the Q-learning agent already do this every single
cycle with zero user input -- worth stating plainly since it's easy to
miss: every loss updates the Q-table's negative reward for that state, and
after 5+ losses on one setup string the risk desk refuses to take it again,
automatically, forever, no command required.

**What was missing:** real traders don't just avoid a bad setup after
enough losses -- they also react *immediately* to the very next trade
after any loss, before there's even enough data for the setup to be
statistically blocked. The standard rule (documented under "anti-martingale"
position sizing): cut size after a loss, only return to full size after a
win. Halving risk by 50% after a loss means a losing streak drains the
account far slower, without needing to know in advance which setup will
go cold.
([FXOpen](https://fxopen.com/blog/en/martingale-and-anti-martingale-strategies-in-trading/),
[FasterCapital](https://fastercapital.com/content/Position-sizing--Optimizing-Position-Sizing-with-Antimartingale-Principles.html))

**Implemented:** `TradeJournal.last_closed_trade()` (most recent closed
trade, any day) + `DeskConfig.reduce_size_after_loss` (on by default in
`funded_account_config()`). In `_consider_entry()`, if the last closed
trade lost money, the next trade's `risk_per_trade_pct` is automatically
halved for that one entry -- logged transparently in the trade reason
("anti-martingale: half size after the last loss"). Fully automatic, runs
every cycle, no user action. Added
`test_reduce_size_after_loss_halves_next_position` (52 tests passing).

## Session 13 (skill search + 2 more real techniques mined from it)

Searched the GitHub skills registry (`npx skills find`) for more trading
skills. Found two, checked both before trusting either:

- `octagonai/skills@forex-list` -- just a static pair listing gated behind
  a paid third-party MCP API key we don't have. Skipped: the bot already
  has a real, session-aware pair list built directly into the code
  (session 9), no external dependency needed.
- `omer-metin/skills-for-antigravity@risk-management-trading` -- mostly
  generic AI-agent-marketplace roleplay framing ("Voice: a veteran trader
  who learned the hard way..."), but its `references/patterns.md` file
  contained standard, correct, textbook risk-management math worth mining
  directly rather than installing the whole persona skill.

**Two techniques pulled from it and implemented for real:**

1. **Tiered drawdown-based position sizing.** The existing
   `MaxDrawdownGuard` was binary: full size right up until the 5% funded
   ceiling, then a hard halt. Real prop desks taper before that cliff.
   Added `MaxDrawdownGuard.size_multiplier(equity)`: under 50% of the way
   to the max-drawdown ceiling, full size; 50-75% of the way there, half
   size; 75-100%, quarter size. Wired into `_consider_entry()`'s position
   sizing in `bots/organization.py`, composing with the existing
   anti-martingale halving from session 12 (both can apply at once).
2. **Risk of ruin, computed from the bot's actual track record.** The
   classic even-money gambler's-ruin approximation (Van Tharp / Ralph
   Vince): `edge = 2*win_rate - 1`, `RoR = ((1-edge)/(1+edge)) **
   (1/risk_per_trade_pct)`. Added `bots.journal.risk_of_ruin()` plus a
   `win_rate` field on `TradeJournal.performance_metrics()`, and put both
   on the dashboard. Explicitly labeled "(est.)" and documented as an
   approximation that assumes roughly 1:1-sized wins/losses -- not exact
   for this strategy's 1:2 R:R shape, but the standard back-of-envelope
   sanity check real desks run. First real number it produced: **~100%
   estimated risk of ruin**, because the all-time win rate across every
   trade ever recorded (including the pre-session-7 buggy crypto period)
   is ~13%, below the 50% break-even point this approximation needs. This
   is an honest number about historical performance, not the current
   forex desk specifically (which has 0 trades closed so far) -- it's a
   real warning sign to watch, not a guess.

Added `test_max_drawdown_guard_size_multiplier_tapers_before_the_halt`,
`test_drawdown_taper_reduces_entry_size`, `test_risk_of_ruin_matches_the_reference_table`,
`test_performance_metrics_includes_win_rate_and_risk_of_ruin` (56 tests passing).

## Session 14 (US30 / NASDAQ added -- MambaFX's actual instruments)

Asked directly to add US30 and NASDAQ. Worth noting: this closes a loop --
session 7 already confirmed MambaFX himself scalps 1-minute NAS100/US30,
not forex majors. The desk switched to forex majors in session 8 for cost
and structural reasons, but there's no reason it can't trade both families
side by side.

**The catch:** "US30"/"NAS100" are broker-CFD nicknames, not real ticker
symbols -- Yahoo has no data under those names. The right instrument for
round-the-clock day trading isn't the cash index either (`^DJI`/`^NDX`
only update during NYSE hours, same problem as trying to session-trade a
market with no real session) -- it's the **futures** contract (`YM=F` for
Dow, `NQ=F` for Nasdaq-100), which trades nearly 24/5 on CME Globex, close
enough to forex hours to run on the same round-the-clock schedule. Both
tickers were already anticipated in `CORRELATION_GROUPS` from session 4
(`YM=F` in us-broad, `NQ=F` in us-tech) but never actually reachable by
typing a name a person would recognize. Verified both fetch real 5-minute
data live before wiring anything up.

**Implemented:** `bots.marketdata.INDEX_ALIASES` + `resolve_symbol()` --
maps `US30`/`DOW`/`NAS100`/`NASDAQ`/`US500`/`SPX` (and a few common
spellings of each) to their real futures tickers. Wired into
`cmd_autopilot` in `bots/cli.py` so `--symbols US30,NAS100,EURUSD` just
works. Also added `US500` (`ES=F`, S&P 500 futures) as the natural third
index alongside US30/NASDAQ -- same family, already grouped in
`CORRELATION_GROUPS`, real institutional volume. The existing
correlation-cluster guard (max 2 positions per correlation group) already
protects against overexposure across DIA/SPY/ES=F/YM=F without any new
code. Added `test_index_alias_resolution` (57 tests passing).

Live desk now watches `EURUSD, GBPUSD, USDJPY, US30, NAS100, US500`
together under the forex-hours clock -- an approximation (CME index
futures actually have a short daily maintenance pause forex doesn't),
stated plainly rather than glossed over, but close enough for a paper
desk and the right side of the approximation (won't sit "closed" during
hours these actually trade).

## Session 15 (stock hours, quieter notifications, Heikin-Ashi)

Three direct asks: stop the phone notifications (dashboard is enough),
also trade stocks during the actual 9:30-16:00 ET NYSE session "like
before," and look into Heikin-Ashi ("forex ashi").

**Notifications:** canceled the win-check PushNotification loop. Replaced
it with a purely silent keep-alive (checks every ~20 min that the live
process didn't die in a container restart, restarts it if so, never
sends a notification or chat reply either way). Dashboard link is the
one source of truth now, as asked.

**Stock hours:** the original stock desk (session 1-4, mega-caps) never
went away, it just wasn't running alongside the current forex/index desk.
Rather than launch a second autopilot process against the same
`paper_state` files (real risk: `PaperBroker`/`MaxDrawdownGuard` do plain
`open(path, "w")` writes, not atomic -- two processes both loading,
modifying, and saving the same JSON around the same moment can silently
lose one process's update), extended the *same* single process instead.
`run_autopilot()` gained a `stock_symbols` param: each cycle, if the NYSE
session is open, those symbols get merged into that cycle's watchlist
alongside the always-on forex/index one -- one process, one writer, same
risk rules for both. Added a matching stock-only `flatten_all(symbols=...)`
so the 4pm close-out only sweeps the stock positions, leaving
still-tradeable forex/futures positions open (they don't need to close
just because NYSE did). CLI: `--stock-symbols` (defaults to
`AAPL,MSFT,NVDA,SPY,QQQ` when `--market forex --funded` are both set).

**Heikin-Ashi:** real technique, not folklore -- averages each candle
with the running trend to cancel out noise, with real, well-documented
caveats: it lags actual price and shouldn't be used standalone or on a
fast entry timeframe, but works well as a *trend filter on a higher
timeframe*. That's exactly the role the session-10 HTF-confirm filter
already plays, so `heikin_ashi()` transforms the higher-timeframe candles
right before `trend_direction()` reads them for that filter -- the fast
1m/5m entry timeframe itself is untouched (where HA's lag would actually
hurt). Falls back to the raw OHLC trend read if the transform fails for
any reason. ([OANDA](https://www.oanda.com/us-en/skills-and-insights/education/technical-analysis/price-charts-and-candlesticks/heikin-ashi-candles-explained/),
[The Forex Geek](https://theforexgeek.com/heikin-ashi-day-trading/))

Added `test_autopilot_adds_stocks_during_nyse_hours`,
`test_autopilot_flattens_only_stock_leg_near_nyse_close`,
`test_heikin_ashi_smooths_noisy_uptrend_into_a_clean_trend` (60 tests
passing).

## Session 16 (exit management: scale-out rejected, time stops adopted, gold added)

Study block while the desk runs live. Two exit-management techniques
researched properly; one adopted, one *rejected with evidence* -- both
outcomes documented because "we checked and the current design is right"
is also a real finding.

**Scale-out (close half at +1R, trail the rest) -- REJECTED.** Compared
against full fixed-target exits on the same strategies, scale-out showed
a 10-25% performance degradation for high-win-rate scalping styles; its
real benefit is psychological comfort, not math. The bot has no
psychology to comfort, so the current fixed 1:2 take-profit stays.
([Traders Second Brain](https://traderssecondbrain.com/guides/take-profit-methods),
[TradeZella](https://www.tradezella.com/blog/scalping-strategies))

**Time stops (N-bar / clock exits) -- ADOPTED.** A scalp signal on 1m/5m
candles bets on a *fast* move; if the trade still hasn't reached +1R
hours later, the market state the signal fired in no longer exists
("state drift") and the position is just parked capital blocking a
position slot and risk budget. Evidence is honestly mixed on raw returns
(some tests show no equity improvement) but consistently shows reduced
drawdown and time-in-market -- and it directly fixes a real observed
behavior: tonight's USDJPY scalp sat flat for hours on a 1-minute
thesis. Implemented as `DeskConfig.max_hold_minutes` (0 = off; 120 min in
`funded_account_config()`): a position that hasn't reached +1R by the cap
is closed on the clock. Trades that DID reach +1R (breakeven-armed) are
exempt -- they're risk-free, the stop/target can finish the job.
([QuantifiedStrategies](https://www.quantifiedstrategies.com/trading-exit-strategies/),
[Nasdaq Playbook](https://nasdaqplaybook.substack.com/p/time-stops-n-bar-exits-when-price))

**Gold added** -- MambaFX's other main instrument alongside NAS100/US30,
and the `gold` correlation group has existed since session 4 with nothing
tradeable in it. `GOLD`/`XAUUSD`/`XAU` now alias to `GC=F` (COMEX gold
futures, near-24h, verified live 5m data). Added to the live watchlist.

Added `test_time_stop_closes_stale_trade`,
`test_time_stop_spares_breakeven_armed_and_fresh_trades`, extended
`test_index_alias_resolution` (62 tests passing).

## Session 17 (MambaFX's actual strategy identified: breakout, not just any signal)

Asked to study MambaFX specifically and trade more like him. His own
channel headline is literal: "MambaFx Breakout Strategy | Easy NAS100 &
US30 Trading Strategy" -- he's a breakout trader on the exact instruments
already added. Backtests on raw opening-range breakouts are damning
though: **65.9% hit their stop, only 34% reach target** -- two out of
three raw breakouts fail. The documented fix, "break and retest": wait
for the level to be revisited and hold before entering, instead of
chasing the breakout candle. Win rates cited moving from 52% to 68% with
this and other confluence filters. Time-of-day data pinpoints exactly
where the chasing problem is worst: 9-10am ET breakout win rates as low
as 30-34%, vs 51-54% by 3-4pm -- the first two hours after any session
open are where raw breakouts fail most.
([XS](https://www.xs.com/en/blog/break-retest-trading/),
[ORB Setups](https://orbsetups.com/research/how-to-identify-and-avoid-false-breakouts-a-data-driven-approach/))

**Implemented:** `orb_chase_filter(df)` in `bots/organization.py` --
computes today's opening-range high/low (first 30 min of the session) and
vetoes an entry if price is already more than 1 ATR past that level
*and* still within the first 2 hours of the session (the specific
high-failure window; trend-continuation trades later in the day are
supposed to be far from the morning's range, that's normal, not chased).
Skips automatically for continuous 24h markets (no real single session to
have an opening range) and for anything past the early window -- a
single-bar approximation of "wait for the retest," not full multi-bar
pattern detection, documented as such. New `DeskConfig.orb_retest_required`
(on by default in `funded_account_config()`). Added
`test_orb_chase_filter_vetoes_extended_early_breakout`,
`test_orb_chase_filter_only_applies_in_the_early_window`,
`test_orb_retest_required_blocks_entry` (65 tests passing).

**Funded-account readiness, asked directly, answered with the actual
numbers instead of a vibe:** the full trade history is 15 real (non-admin,
non-zero) closed trades across the entire project's life: 1 win, 9
losses (5 more are $0.00 bookkeeping closes from broker migrations, not
trading decisions). Win rate 6.7%. Risk of ruin (per session 13's
formula): 100%. **Not remotely enough data or evidence to trust yet** --
almost that whole history predates this session's fixes (sessions 7-17:
signal-repaint fix, continuous-market fix, session-aware pairs, HTF
confirm, anti-martingale sizing, drawdown taper, time stops, breakout
retest filter). The *current* rule set has closed **zero** trades of its
own so far. The honest bar before considering a real funded account:
enough closed trades under the current code (not the old buggy version)
to make win rate/expectancy statistically meaningful -- 30-50 trades
minimum, spanning multiple sessions/days/market conditions, with
`risk_of_ruin()` sitting comfortably low rather than at the "certain
ruin" end, and the 3%/5% funded limits never once breached across that
whole stretch. Not a fixed calendar date -- a data bar, tracked live on
the dashboard (win rate, profit factor, risk of ruin chips already
there) as it actually gets crossed.

## Session 18 (widen the watchlist instead of loosening the filters, auto mistake log, fixed a real correlation-cap bug)

Asked to trade more right now. Checked the actual cycle log first instead
of guessing: 16 skips, 0 new entries over 5 cycles. Each skip had a real
reason (off-session pair, bearish read, trend disagreement) -- the desk
wasn't broken, it just only had 3 forex pairs to choose from (EURUSD,
GBPUSD, USDJPY) while the session-awareness system (session 9) actually
knows about 12. Most cycles, the 3 pairs on the watchlist just happened
to be the wrong ones for whatever session was live. Fix: widen the
candidate pool to match what the filter already understands, rather than
loosen any filter's standard. More shots on goal at the same bar, not a
lower bar.

**Live watchlist grew from 3 forex pairs to all 12** (`EURUSD, GBPUSD,
USDJPY, AUDUSD, NZDUSD, USDCHF, USDCAD, EURJPY, GBPJPY, AUDJPY, EURGBP,
EURCHF`) plus the existing indices/gold. All 12 verified to fetch live
5-minute data before adding.

**Bug found and fixed while doing this:** `CORRELATION_GROUPS["usd-fx"]`
only listed 3 pairs, and the session-aware skip filter was checking
`correlation_group(symbol) == "usd-fx"` to decide "is this a forex pair
at all" -- so any of the 9 new pairs outside that one group would have
silently skipped the session check entirely (correlation grouping and
session-awareness are two separate concerns that had gotten conflated
through a shared string). Fixed two ways: split `usd-fx` into three real
correlation clusters (`usd-fx` for direct USD pairs, `jpy-crosses` for
non-USD yen pairs, `eur-crosses` for EUR-driven non-USD/non-JPY pairs --
the actual macro drivers that move these together), and decoupled the
session-filter's "is this forex" check into its own
`ALL_SESSION_FOREX_PAIRS` set, independent of correlation grouping.
Without this fix, the correlation cap (max 2 per cluster) would have
stopped covering 9 of the 12 pairs the moment the watchlist grew --
exactly the "hidden leverage" gap this system exists to prevent.

**Auto mistake-logging, no chat involved:** asked for review-your-mistakes
to happen automatically without being asked, and without a chat reply
each time. The Q-agent's negative reward and `should_avoid()`'s setup veto
already did the *mechanism* silently, every cycle, with zero LLM calls --
worth restating since it's easy to assume "auto-improve" requires an AI
in the loop; it doesn't, it's plain code. What was missing was a
*readable trail* of it. `TradeJournal.close_trade()` now writes one line
to `mistakes_log.md` the instant any real (non-admin) losing trade
closes -- pure Python string formatting inside the journal itself, not a
chat message, not a Claude Code turn. Runs whether or not anyone's
watching.

Added `test_close_trade_logs_losses_to_mistakes_file`,
`test_session_aware_forex_applies_to_non_usd_crosses_too` (67 tests
passing).

## Session 19 (crypto weekend fallback reversed on fresh evidence, oil added)

User's instinct: "crypto is trash, there's better." Checked with current
(2026) data instead of relying on session 8's older research.

**Weekend crypto fallback -- REVERSED.** Session 11 added crypto as the
weekend trading fallback since forex/indices/gold all close Friday
evening to Sunday evening. Fresh data changes the calculus: since spot
ETFs launched, institutional market-making has concentrated into weekday
hours, and weekend crypto liquidity has gotten *worse*, not better --
trading costs +11%, effective market depth -9%, displayed liquidity -5%
vs weekdays. Real consequence cited: a Feb 1, 2026 Saturday afternoon
selloff cascaded into $2.2B in liquidations across 335,000 traders in 24
hours, specifically because weekend order books were too thin to absorb
it. **Decision: stop auto-enabling the crypto weekend fallback.** The
mechanism (`--weekend-symbols`) stays available for anyone who wants to
opt in explicitly, but the desk no longer reaches for it on its own --
the better choice, given this account has no real track record yet, is
to simply not trade over the weekend rather than force activity into a
demonstrably thinner, worse market than it used to be.
([Phemex](https://phemex.com/blogs/weekend-crypto-trading-explained),
[Blockearner](https://blockearner.com.au/blog/the-bitcoin-liquidity-gap-why-the-24-7-crypto-market-gets-volatile-when-wall-street-logs-off/))

**Oil added.** WTI crude (`CL=F`) is the world's most liquid crude
contract -- 1M+ contracts/day, ~4M open interest -- and already had an
unused correlation-group slot from session 4. Verified live 5-minute data
before adding. `OIL`/`WTI`/`CRUDE`/`USOIL` alias to `CL=F` in
`bots.marketdata`. Added to the live watchlist.

Extended `test_index_alias_resolution` for the new oil aliases (67 tests
still passing -- the weekend-fallback change is a CLI default change, not
new logic, so no new test needed beyond the existing
`test_autopilot_weekend_crypto_fallback` which still verifies the
mechanism itself still works when explicitly requested).

## Session 20 (found and closed a real gap: the Q-agent wasn't actually learning live)

Asked directly whether the bot learns from mistakes "automatically,
infinitely." Checked the code instead of assuming yes, and found the
honest answer was "half of it."

**What was already true:** the journal's setup veto (`should_avoid()`)
re-reads real closed-trade stats every single cycle with zero retraining
step -- genuinely continuous, automatic, unbounded. Same for the
anti-martingale sizing (session 12) and drawdown taper (session 13).

**What wasn't:** the Q-learning agent's actual table -- its "opinion" of
each market state -- only ever got updated inside `_run_episode()`,
called only by the offline `train` CLI command on historical data. Live
trading only ever called `agent.signal()` (a pure lookup) and
`agent.current_state()`, never `agent._update()` (the real learning
step). So the model itself was frozen between manual retrains; it was
using experience, not learning from it, while live.

**Fixed:** `TradingDesk._online_learn()` in `bots/organization.py`, called
right after every real trade closes in `_manage_position()`. Recovers the
entry-time state from the journal record's `setup` string (already
recorded there for exactly this), computes the post-close market state as
the TD target, and calls the *same* `_update()` method training already
uses -- same learning rate, same math, just fed by real outcomes instead
of backtest replay. Admin-tagged and manual/mirror trades (no recorded
state) are correctly skipped. Wrapped in try/except: a learning-update
failure must never be able to break a trade close.

Added `test_closing_a_trade_updates_the_qtable_live` (confirms the
Q-value actually moves and persists to disk) and
`test_online_learning_skips_admin_and_manual_trades` (69 tests passing).

## Session 21 (a narrow, bounded exception to the daily trade cap)

Asked for the bot to still trade a genuinely good setup even after
hitting the daily cap, instead of sitting out for the rest of the day no
matter what. Fair ask -- a hard count limit doesn't know the difference
between a mediocre 10th trade and an exceptional one. Implemented as a
narrow exception, not a removed rule:

`DeskConfig.high_conviction_adx` (40.0 in `funded_account_config()`) --
ADX 20 is the normal "trending, not choppy" floor already used elsewhere;
40+ is Wilder's classic "very strong trend" tier, a real step up rather
than just clearing the bar. When the daily cap is hit, a candidate whose
ADX clears this higher bar gets a second chance -- but it still has to
pass every other filter (RL signal, HTF confirm, ORB retest, ADX regime,
correlation cap, all of it); this only lifts the *count* limit, nothing
else. Bounded by `max_high_conviction_overrides` (2/day) so it can't
quietly become an unlimited hole in the discipline -- `TradeJournal`
tracks how many have fired today via a `high-conviction-override` tag,
same pattern as the existing admin-tag exclusions. Every override trade
is logged transparently in its reason
("high-conviction override: exceptionally strong trend, bypassed the
daily trade cap") so it's visible on the dashboard, not hidden.

Added `test_high_conviction_override_bypasses_daily_cap`,
`test_high_conviction_override_disabled_by_default_still_caps`,
`test_high_conviction_overrides_run_out_per_day` (72 tests passing).

## Session 22 (self-review found a real scheduling flaw: the "day" rolled at the wrong time)

Reviewed the bot's own behavior instead of adding features: at 10:50 UTC
the desk was skipping every candidate with "daily trade cap reached" --
right as London was open and the London/NY overlap (its own session-9
research says this is the single best window of the day) was approaching.
Looked at the journal: all 12 of the day's trades were opened between
00:36 and 06:47 UTC, i.e. the overnight Asian session, the *worst*
liquidity window. The budget was spent before the good hours even
started.

**Root cause:** every per-day counter (trade cap, loss streak,
high-conviction overrides, daily circuit breaker, profit target) keyed
off UTC midnight. But the forex/funded-account convention rolls the
trading day at **5pm New York time** (the daily close) -- under that
boundary, an overnight Asian session and the following London/NY
sessions belong to the *same* day and share one budget, which is exactly
how a prop firm counts "daily" too. Under UTC midnight they were split
in half at the worst possible point.

**Fixed:** `bots.journal.trading_day(dt)` -- maps any timestamp to its
trading day with the 5pm-ET roll (NY time + 7 hours, date part). Now used
by `trades_opened_today`, `count_trades_with_tag_today`,
`consecutive_losses_today`, and both `DrawdownGuard.check()` and
`day_gain_pct()` (daily loss limit + profit target now reset at the same
instant a funded firm's would). Malformed timestamps fall back to the old
date-prefix behavior rather than crashing.

**Honest second look -- the boundary fix alone wasn't enough.** Under
either boundary (UTC midnight or 5pm ET), the trading day still *starts*
with the Asian session, so a flat per-day cap can still be fully spent
overnight before London opens. The boundary change is still correct (it
matches how a funded firm counts "daily," so the loss limit and profit
target now reset at the right instant), but the observed problem needed a
second fix: **a session budget**. New `DeskConfig.asian_session_budget_pct`
(0.4 in `funded_account_config()`): while the Asian session is the live
one, only 40% of `max_trades_per_day` may be spent; the remaining 60% is
reserved for London/NY -- directly encoding the session-9 finding that
the overlap is where the edge concentrates. High-conviction overrides
(session 21) still work during the Asian session, so a genuinely
exceptional overnight setup isn't locked out entirely. The skip message
says exactly what's happening ("session budget: 4 of 10 daily trades
allowed during the thin Asian session -- saving the rest for London/NY").

Also re-ran the skills-registry search (asked to look for more): the
risk/forex results were the same two skills already mined in session 13,
and the trade-journal skills found (one for Chinese A-stocks, one
already-reviewed Solana-heavy marketplace, one with 22 installs) all
cover less than the journal already does (setup grading, auto mistakes
log, profit factor, risk of ruin). Skipped honestly instead of
installing for show.

Added `test_trading_day_rolls_at_5pm_new_york` and
`test_asian_session_budget_reserves_trades_for_london` (74 tests passing).

## Session 23 (checked before raising risk, researched RSI-overbought entries, one real token-saving find)

Asked to take on more risk "if it means money comes in." Checked the
real numbers before touching anything: **last 25 real trades, 48% win
rate, net -$7.10.** That's not a track record that supports taking MORE
risk -- it's roughly break-even-to-slightly-negative. **Declined to
increase risk_per_trade_pct.** The standing rule from session 11 still
holds: risk changes get sized by what the data says, not by what would
feel good. Will revisit if/when the real numbers actually support it.

**Researched RSI-overbought entries** (prompted by the mistakes log
showing a cluster of stale/losing overbought-entry trades). Real
internal data: 18 overbought-entry trades, net -$3.23, but the picture is
noisy -- 3 of 5 specific sub-patterns are flat-to-profitable, only one
(already blacklisted by the existing setup-veto after 6 straight losses)
is clearly bad. External research is genuinely mixed too: overbought
RSI in a strong uptrend is often continuation, not a reversal warning --
"RSI above 70 only signals a potential reversal when accompanied by
divergence or a failure swing," and pullback entries (RSI cooled to the
40-55 zone) are the well-evidenced lower-risk re-entry, not "never buy
overbought."
([Stockcharts](https://articles.stockcharts.com/article/stop-thinking-of-rsi-as-overbought-and-oversold/),
[GoatFundedTrader](https://www.goatfundedtrader.com/blog/best-rsi-settings-for-day-trading))
**Decision: no new filter.** The evidence doesn't clear the bar for a
blanket rule, and the one setup that clearly is bad is already
handled by the existing per-setup veto. Documented as a real "looked,
found it's genuinely ambiguous, didn't force a change" outcome.

**Skills search for token-usage reduction** (asked again, searched hard
this time). Most results were the same kind of "cuts usage by X%" framing
already debunked earlier in this project. One (`dubibubii/usage-limit-reducer`)
turned out legitimate on inspection -- not a magic-number claim, just
real, known practices (fresh sessions, right-sized model per task, track
actual usage, reuse recurring context via a project file). That last one
was a real, actionable gap here: **this repo had no `CLAUDE.md`.** Added
one -- captures the `BOT_DATA_DIR=paper_state` convention, the exact live
desk launch command, test/dashboard commands, and the change-and-restart
workflow, so a fresh session (including scheduled routine fires) doesn't
have to re-derive all of this from scratch every time. That's a real,
if modest, token saving -- not a 90% headline number, an honest one.

No code changes to `bots/` this session (research + a docs/CLAUDE.md
addition only) -- no test changes, no live-desk restart needed.

## Session 24 (historical stress test -- real bad days, not a fake trade)

Asked to speed up validation by having the bot "practice" on a bad
market. Explicitly declined the version of this that would have been
dishonest: injecting a fabricated trade into the live journal to "teach"
the model would poison both the Q-learning update (a lesson learned from
a market event that never happened) and the real track record the whole
funded-account-readiness question depends on. Built the honest version
instead: `scripts/stress_test.py` replays the **actual** live desk
(`TradingDesk` + `funded_account_config()`, same code, same filters) bar
by bar against **real** historical 5-minute price data, entirely in a
throwaway temp directory that can never touch `paper_state/`.

Two real bugs found and fixed while building it (both about the
simulation being lookahead-free and fast, not about the trading logic):
the higher-timeframe confirm filter (session 10) defaults to a live
network fetch, which in a historical replay would silently use *today's*
real market data against a simulated past "now" -- fixed by resampling
the same historical frame instead. And a naive per-symbol replay loop
made one live network call per simulated cycle, ~1700 times -- switched
to pre-fetching all history once and slicing in memory.

**Ran it across the actual live watchlist (8 symbols) over the roughest
real week available in the last 60 days (June 18-23) combined across all
of them.** Result: 6 trades, worst peak-to-trough dip **0.27%**, safety
limits never triggered. Two honest readings of that number, both stated:
either the desk's own entry filters (session-aware pairs, ADX regime,
HTF confirm, breakout-retest) are doing their job keeping it out of bad
conditions before the hard stops would ever be needed -- or the roughest
window free 60-day data can show still isn't a real crisis, so the 3%/5%
limits remain *unproven under real stress*, only proven correct in
isolation (the existing unit tests already verify the guard math itself
with synthetic drawdown sequences -- `test_max_drawdown_guard_halts_and_stays_halted`,
`test_desk_halts_on_total_drawdown_breach`). Both things are true at
once; neither one is "the bot is definitely safe in a crash."

## Session 25 ("what's the best thing to trade right now" ranker + practice expanded to 3 rough windows)

Asked for the bot to know, at any moment, which instrument is the best
one to trade. Research confirms the standard professional recipe is
exactly three components: **volatility** (enough movement to pay for the
trade), **liquidity** (tight spreads, real volume), and **trend
strength** -- and the desk already measured all three separately (ATR,
session scores, ADX); they'd just never been combined into an actual
ranking.
([daytrading.com](https://www.daytrading.com/strategies),
[TradeAlgo](https://www.tradealgo.com/trading-guides/day-trading/best-day-trading-indicators-the-7-technical-tools-professional-traders-actually-use))

**Implemented `tradeability_score(symbol, df)`** in
`bots/organization.py`: trend strength (ADX/25, capped 2.0) + movement
potential (ATR% scaled, capped 2.0) + session liquidity (the existing
0-2 forex session score; futures get a neutral 1.0 as near-24h markets).
Every cycle, all candidates are ranked by it and the best market gets
first claim on the open slots; the full ranking is printed in the cycle
notes ("[rank] best to trade right now: ..."). Two important properties:
the score only ORDERS candidates -- every hard filter (ADX floor, HTF
confirm, session skip, correlation cap, breakout-retest) still applies
unchanged after ranking -- and manual mirror calls keep first claim
regardless of score (a human's explicit call isn't outranked by the
auto-ranker). Also added a per-cycle history cache so ranking adds zero
extra network fetches (one fetch per symbol per cycle, reused by the
entry logic; previously the entry logic fetched separately).

**Practice mode expanded** (`stress_test.py --practice`): now trains the
live model on the top **3** roughest real windows in the last 60 days
(June 22, June 10, June 11), not just the single worst. Ran it: across
most windows the trained policy's final answer was "don't trade" -- 0
eval trades -- which is itself the correct learned response to bad
conditions; where it did probe (CL=F, NQ=F) it experienced negative
outcomes and pushed those state judgments down. Same guarantees as
session 24: Q-table only, never touches the journal or any
readiness-grading number.

**Asked-for-but-not-done, stated plainly:** "ready for the funded
account by tomorrow" -- no. The readiness bar hasn't changed (30-50
real trades on current code, healthy risk-of-ruin, limits never
breached, ideally one real rough day survived). Practice on historical
windows accelerates the model's pattern knowledge but does not count as
live evidence. Also "study until 90% token usage" -- burning tokens is
not a goal; the study is done when the genuinely useful improvements are
implemented, which this session did.

Added `test_tradeability_ranking_gives_strong_trend_first_claim`
(75 tests passing).

## Session 26 (real mistake found: winners were being cut shorter than losers)

Asked "how could you have made more than $4" -- checked the real trade
log instead of reaching for "add more risk." Last 30 real trades: 56.7%
win rate (genuinely good), but **average win $0.88 vs. average loss
$1.04** -- backwards from the 1:2 reward:risk shape the whole stop/target
system is built around. Root cause, found in the actual exit reasons:
almost every winning trade in the log closed with "quant desk: RL agent
says exit," not "take profit hit."

**The bug:** `_manage_position()`'s exit checks ran in order (stop loss,
breakeven, take profit, time stop, *then* the RL exit signal as a
catch-all) -- but the RL check had no floor. It could fire on a trade
already breakeven-armed (proven to be +1R working) and cut it at a small
fraction of the real target, every time, systematically. This is the
same lesson session 16 already found from a different angle (fixed
targets beat early trimming for this kind of system) showing up again
through a different mechanism.

**Fixed:** the RL exit signal is now only honored *before*
breakeven-armed. Below that point it's still a legitimate early bailout
("this thesis looks like it's turning bad, get out before it's a bigger
loss"); once a trade has proven itself, the RL signal can no longer
override the structured breakeven-stop/take-profit management that's
specifically there to let a real winner run to target. Not a risk
increase -- a bug fix in how existing profit was being realized.

Added `test_rl_exit_does_not_cut_a_breakeven_armed_winner_short` and
`test_rl_exit_still_works_before_breakeven_armed` (77 tests passing).

## Session 27 (24/7 restored with an evidence-linked safeguard, and a real gap found: zero spread cost)

**24/7, not 24/5 -- reconciled with session 19's evidence rather than
either ignoring it or refusing.** The weekend crypto fallback stays
removed *as an unguarded default* was the honest call in session 19, but
"never trade weekends at all" isn't the only honest option once there's
a way to price in the documented risk instead of pretending it doesn't
exist. Added `is_weekend_forex_gap()` and
`DeskConfig.weekend_crypto_caution` (on by default): weekend crypto
trades now automatically get **half size**, specifically because of the
session-19 findings (+11% costs, -9% depth since spot ETFs concentrated
weekday liquidity). Re-enabled the `--weekend-symbols` default
(BTC-USD/ETH-USD) in `cmd_autopilot`. Net effect: genuine 24/7 coverage,
with the weekend risk explicitly priced in rather than either denied or
ignored.

**Real gap found while studying what the bot doesn't account for yet:**
`PaperBroker` filled every single order at the exact quoted mid-price --
zero spread cost. Every number on the dashboard (win rate, P&L, risk of
ruin) had been computed as if trading were frictionless, which no real
account is. Research confirmed this is a well-documented, common paper-
trading blind spot: "a strategy with a 1.5 Sharpe ratio in a frictionless
backtest may collapse below 0.5 after realistic fill modeling."
([QuantMedia](https://quantmedia.io/paper-slippage-latency-modeling.html),
[Substack](https://algorithmictoken.substack.com/p/market-structure-lens-1-the-cost))

**Implemented `bots/spreads.py`**: category-based spread-cost lookup
sourced from real typical retail/institutional spread data (forex majors
~1 pip, JPY crosses and forex crosses wider, index futures 1-2 ticks
very tight relative to price, gold/oil wider, crypto 3-5x forex and
doubled again on weekends via the same `is_weekend_forex_gap()`).
`PaperBroker` gained an opt-in `model_spread` flag -- **off by default**
so the 79 existing tests' exact expected fill-price assertions stay
completely unchanged, **on** for the live desk (`cmd_autopilot` and
`cmd_trade`, both wired) so its numbers now reflect real transaction
cost instead of a systematically optimistic frictionless fill. Buys fill
at the modeled ask (above mid), sells at the modeled bid (below mid) --
standard bid/ask fill modeling, not a flat fee.

Added `test_paper_broker_default_has_no_spread_cost`,
`test_paper_broker_model_spread_charges_realistic_cost`,
`test_spread_pct_widens_crypto_on_weekends`,
`test_is_weekend_forex_gap`, `test_weekend_crypto_trades_get_half_size`
(82 tests passing).

## Session 28 (verified the funded-account safety net before, not after, connecting one)

Asked to make sure the safety guards won't fail when a real funded
account (TradeLocker) gets connected. Flagged a possible gap in the
previous reply -- rather than just asserting it was fixed, verified it
properly this time before doing anything else.

Turned out the isolation was already correct: `TradingDesk.__init__`
already namespaces both drawdown guards' state files by
`self.broker.name` (`day_state_tradelocker.json`,
`max_drawdown_state_tradelocker.json` -- separate from the paper
account's files, so connecting a new broker can never read a stale or
wrong-scale baseline, the exact "-900% false drawdown" class of bug from
early in this project). What actually mattered was proving it and
writing down the one real condition it depends on: those files only
survive a container restart if `BOT_DATA_DIR` points at the
git-committed `paper_state/` directory -- pointing a new broker at the
default gitignored `bot_data/` instead would put its guard memory
somewhere that doesn't survive a restart, silently weakening the
protection. Added `test_guard_state_isolated_per_broker_and_respects_bot_data_dir`
(no real TradeLocker credentials needed -- a fake broker with
`.name = "tradelocker"` proves the same code path) and made this
explicit in `CLAUDE.md` so it can't be forgotten when TradeLocker is
actually connected. `TradeLockerBroker` itself (`bots/brokers/
tradelocker_broker.py`) already existed from an earlier session, defaults
to TradeLocker's demo environment, and only touches real money if
`TRADELOCKER_LIVE=1` is explicitly set.

83 tests passing.

## Session 29 (proved two funded accounts of the *same* broker type stay fully separate, ahead of buying them)

User is about to buy two separate funded TradeLocker accounts (different
logins, same MambaFX-referenced rule set as before: 10x leverage, 3%
daily loss, 5% max drawdown) and wants both traded independently, "like
two bots."

Session 28 proved isolation across different broker *types* (paper vs.
a fake `tradelocker`). That's not the same claim as two accounts of the
*same* type -- both real accounts here would report `broker.name ==
"tradelocker"`, so it was worth checking the isolation logic doesn't
secretly key off broker type/name colliding before either account was
ever real. It doesn't: `TradeJournal()`, `QTraderAgent()`,
`DrawdownGuard`, and `MaxDrawdownGuard` all resolve their file paths via
`bots.paths.data_path()`, which reads `BOT_DATA_DIR` from the
environment at call time -- not from anything broker-specific. So the
actual isolation boundary is "one `BOT_DATA_DIR` per process," which
composes cleanly: two processes, two `BOT_DATA_DIR` values, zero shared
state, regardless of both brokers sharing the name `tradelocker`. No
code changes were needed -- this is architecture that already existed,
just unverified for this exact shape of the scenario.

Also confirmed `TradeLockerBroker` has no local-file persistence to
worry about (unlike `PaperBroker`'s `paper_account.json`) -- it reads
account state live from TradeLocker's API via `TLAPI`, so there's no
extra file to accidentally point at the same path across two accounts.

Added `test_two_funded_accounts_of_the_same_broker_type_stay_fully_separate`:
two `FakeFundedBroker(PaperBroker)` instances, both `.name =
"tradelocker"`, constructed under two different `BOT_DATA_DIR` values;
account 1 takes a loss and its guard records a 4% daily drawdown;
asserts account 2's journal path, guard state path, and Q-table model
path are all distinct from account 1's, and that account 2's guard is
still flat/unhalted -- proving one account's losses can never bleed into
the other's limits.

**Practical upshot for running two accounts:** two fully separate
`BOT_DATA_DIR` directories, two independent `python -m bots autopilot
--broker tradelocker --funded ...` background processes, each with its
own `TRADELOCKER_EMAIL` / `TRADELOCKER_PASSWORD` / `TRADELOCKER_SERVER`
env vars. Both default to TradeLocker's demo environment until
`TRADELOCKER_LIVE=1` is explicitly set per-process.

84 tests passing.

## Session 30 (funded-account go-live prep: TradeLocker connector hardening + preflight + two-bot launcher)

User hands over the two funded TradeLocker accounts tomorrow. Audited the
connector the way a real fill would exercise it and found three genuine
gaps -- each one a way a funded account could get hurt operationally even
with perfect strategy code:

1. **No server-side stop-loss.** `TradeLockerBroker` had no
   `buy_bracket`, so entries fell back to a plain market buy and ALL
   protection lived in the desk's polled stop checks. This container
   restarts frequently; a restart with an open unprotected position means
   no stop at all until the keep-alive notices. On a 3% daily-loss
   account that is the single most likely blow-up path. Implemented
   `buy_bracket` using TLAPI's `stop_loss`/`take_profit` with
   `type="offset"` (measured from the actual fill, not the pre-order
   quote). Deliberate policy: if the bracket is rejected there is NO
   fallback to an unprotected buy -- a missed trade is recoverable, an
   unprotected funded position is not.

2. **Exits could have opened shorts.** Prop-firm TradeLocker accounts
   default to hedging mode, where a naked sell order OPENS a short
   alongside the long instead of closing it (doubling margin and leaving
   two exposed positions). `sell()` now goes through TLAPI's
   `close_position` endpoint against the account's actual open long
   positions (partial closes pass the partial quantity; full closes pass
   0 = whole position), and only falls through to a plain sell order if
   there is genuinely nothing to close.

3. **Symbol renames looked like closed trades.** Firms name CFDs
   differently (gold is usually XAUUSD, Nasdaq can be US100/USTEC...).
   The desk journals "GOLD"; if positions() reported "XAUUSD" the desk's
   reconciler would treat GOLD as an orphan whose bracket leg must have
   fired and close the journal entry -- while a real position sat open
   unmanaged. Added `TRADELOCKER_ALIASES` (forward resolution, first
   name the firm recognises wins, cached) and reverse mapping in
   `positions()` so both directions agree with the journal, including
   after a cold restart with a position already open. Same fix covers
   the weekend fallback's Yahoo-style names (BTC-USD <-> BTCUSD).

Go-live tooling:
- `scripts/preflight_funded.py`: read-only connection check -- auth,
  balance/equity, existing positions, resolves all 17 watchlist symbols
  to this firm's instrument names with live ask prices and min lot
  sizes. Optional `--order-test` (refuses on live) does one smallest-size
  EURUSD bracket round-trip on demo: order in, position visible under
  the desk's name, closed via the position endpoint, confirmed gone.
- `scripts/run_funded_accounts.sh`: launches both account bots as fully
  isolated processes (BOT_DATA_DIR=funded_state_acct1/2, credentials
  from gitignored bot_data/tradelocker_acct{1,2}.env), and refuses to
  start any bot whose preflight fails.
- `funded_state_acct1/` + `funded_state_acct2/` committed so each
  account's journal/Q-table/guard limits survive container restarts from
  day one (CLAUDE.md updated with the convention).

Tests (mocked TLAPI, no credentials or network):
`test_tradelocker_demo_by_default_and_alias_resolution`,
`test_tradelocker_positions_map_back_to_desk_names_after_restart`,
`test_tradelocker_bracket_attaches_stops_and_never_enters_unprotected`,
`test_tradelocker_sell_closes_position_instead_of_opening_short`,
`test_tradelocker_weekend_crypto_round_trips_to_journal_name`.

Still demo-first: nothing trades real money until `TRADELOCKER_LIVE=1`
is set per account, and that only happens after the demo run has proven
itself. 89 tests passing.

## Session 31 (bad-market survival: loss-budget headroom cap + rollover blackout)

Directive: study bad markets every way possible before tomorrow's funded
handoff. Researched how funded accounts actually die, then closed the two
gaps the evidence pointed at.

**Research findings (real sources, not folklore):**
- One prop-firm operator reports ~78.7% of ALL challenge failures are
  daily-drawdown breaches -- not max drawdown, not rule violations: the
  daily line. The standard professional mitigation is budget discipline:
  never consume more than ~80% of the daily limit; the last ~20% is
  insurance against slippage/spread widening, because stops are NOT
  guaranteed fills (cleo.finance breach-analysis, FXNX buffer-strategy
  guides, The5ers/ThinkCapital drawdown explainers all converge on this).
- The 5pm ET daily rollover is a documented liquidity vacuum: banks pause
  pricing, spreads on even EURUSD can hit ~20 pips (Forex Peace Army
  thread with broker data, FOREX.com rollover FAQ), and a funded-account
  help center (FundingPips) explicitly warns the widened quote alone can
  trigger stops and daily-loss breaches with no real price move. The CME
  futures venues behind US30/NAS100/US500/GOLD/OIL literally pause 5-6pm
  ET every day.

**The gap this exposed in our own desk:** sizing knew risk_per_trade_pct,
anti-martingale, and the drawdown taper -- but nothing connected a NEW
trade's risk to how much of today's 3% was already gone. Concrete failure:
day at -1.6%, normal 1.5%-risk entry, stop hits -> -3.1% -> funded account
terminated. The DrawdownGuard's halt only fires AFTER the line is crossed;
for a prop account that is one cycle too late. This is precisely the
"normal-sized trade late in a red day" breach pattern the research says
kills most accounts.

**Implemented:**
- `DrawdownGuard.loss_headroom_pct(equity, budget_pct)` and
  `MaxDrawdownGuard.loss_headroom_pct(...)`: remaining tradeable risk (as
  a fraction of current equity) before consuming `budget_pct` (default
  80%) of each limit.
- `_consider_entry` now caps every trade's risk_pct to the smaller of the
  two headrooms and refuses entries outright once a budget is spent --
  BEFORE the hard halt would trip, with the last 20% never knowingly
  risked. Applies after all other sizing adjustments so it is a true
  ceiling, never an increase.
- `DeskConfig.loss_budget_pct = 0.8` (on for every config),
  `DeskConfig.rollover_blackout` (default off, ON in
  funded_account_config): no new non-crypto entries 4:45-6:15pm ET.
  Crypto is exempt (24/7 venue, no rollover). Exits/server-side stops
  unaffected. Stress-test replay disables it alongside news_blackout
  (both key off real wall-clock, meaningless against historical bars).

Tests: `test_daily_loss_headroom_shrinks_as_the_day_gets_worse` (proves
headroom hits zero BEFORE the 3% halt line),
`test_entry_risk_is_capped_to_remaining_daily_headroom`,
`test_max_drawdown_headroom_caps_toward_the_account_ceiling`,
`test_rollover_window_detection`,
`test_rollover_blackout_blocks_forex_entries_but_not_crypto`.

94 tests passing.

### Session 31 addendum: Clarity Traders' actual rule sheet (fetched from their own FAQ)

Since the funded accounts are being bought from MambaFX's firm (Clarity
Traders), fetched their FAQ directly rather than trusting review-site
summaries (which contradicted each other on the important points):

- **Bots require a paid add-on**: "Automated trading and Expert Advisors
  are allowed only if you have purchased the EA's Allowed add-on." HFT
  and latency arbitrage are banned outright (we are neither -- 1-minute
  polling). ACTION FOR PURCHASE DAY: both accounts MUST include the
  "EA's Allowed" add-on or the bot itself is a rule violation.
- **News trading is permitted** ("traders are responsible for managing
  increased volatility, slippage, and execution risks") -- so the +/-10min
  entry blackout stays as risk discipline, and no flatten-before-news
  logic is required for compliance at this firm.
- **Weekend holding/trading is banned without the "Trade on Weekends"
  add-on**: "Without this add-on, all positions must be closed before
  market close on Friday." TWO code changes shipped for this:
  `DeskConfig.friday_flatten` (ON in funded config) closes every
  non-crypto position in the 4:30-5:00pm ET Friday window and blocks new
  entries until the close; and `--weekend-symbols none` (used by
  scripts/run_funded_accounts.sh) fully disables the weekend crypto
  fallback on the funded accounts, since ANY weekend trade would break
  the rule. The paper-trading desk keeps its 24/7 crypto fallback -- this
  split is per-account-rules, not global.
- **Instant account consistency rule**: no single day may exceed ~10% of
  a requested payout (their example: $1k/day max toward a $10k payout).
  Payout-shaping, not a termination trigger; the 3% daily profit target
  already caps day size. Documented for payout planning, no code change.
- Their Instant account limits (3% daily / 5% overall) exactly match the
  rules screenshot and funded_account_config's numbers.

Tests added: `test_friday_close_window_detection`,
`test_friday_flatten_closes_positions_before_the_weekend`,
`test_weekend_symbols_none_disables_fallback` (via the new
`bots.cli.resolve_weekend_symbols` helper, extracted so the actual CLI
logic is what gets tested).

97 tests passing.

## Session 32 (train in the past on BOTH regimes; copy-trading legality researched -- critical finding)

Directive: stop letting the bot idle through quiet live markets ("one whole
day for one day of experience") -- go back in time and get reps in both the
disaster days AND the big-money days; and look into copy trading
(MambaFX-style leaders, the "$4k/week" copy services).

**Time-machine training extended to the good days.** `find_best_trend_days`
added to scripts/stress_test.py: ranks real historical days by combined
NET directional move (big runner days), complementing `find_roughest_days`
(range/chop days). `--practice` now trains the live Q-agent on the top
rough windows PLUS the top trend windows (deduped). Rationale: the two
regimes that decide a scalper's month are the days that can hurt it and
the trend days it must not waste -- practicing only on rough days taught
defense but never offense. Same isolation guarantees as before: only the
Q-table is touched, never the journal/track record.

**Copy trading: researched before wiring anything, and the answer is a
hard stop for the funded accounts.** Consistent across 2026 prop-industry
sources (Tradeify, Apex, NexusFi, trade-copier operator guides):
- Copying YOUR OWN strategy across YOUR OWN accounts: allowed at most
  firms -- this is exactly the two-bot setup already built (each account
  runs our own desk logic independently). No change needed.
- EXTERNAL signals -- subscribing to another trader's calls (MambaFX or
  anyone), paid signal groups, pass-your-challenge services: banned
  essentially everywhere, detected via timestamp/instrument/size
  fingerprinting across accounts, punished with termination AND denied
  payouts. The "$4k in a week copy trading" pitches are exactly the
  category that gets funded accounts killed.
Decision: on the funded accounts the bot trades ONLY its own signals.
MambaFX's *style* stays absorbed as strategy research (ORB retest filter,
session discipline -- already implemented in earlier sessions); his
*trades* are never mirrored there. The manual mirror mode remains
available for the PAPER account only.

**Tried the available `trading-signal` skill** (on-chain smart-money
buy/sell events, BSC/Solana): returned an empty result set when called;
and even working, it surfaces memecoin flow -- wrong market for a forex
prop desk, and feeding any external signal into the funded accounts is
the banned pattern above. Evaluated and rejected on evidence, same
treatment as every other shiny object.

### Session 32 addendum: deep-hourly training tried and REJECTED (numbers included)

Ran the new `--practice-deep` mode: 8 instruments x up to ~17k hourly bars
(Oct 2023 -> Jul 2026) x 6 episodes -- by far the largest training run to
date. Results argued against keeping it:

- **+0 new states.** The state abstraction saturates at 220 states; 2.3
  years of hourly bars only re-weighted existing Q-values, it taught the
  agent nothing structurally new.
- **Raw greedy policy was strongly net-negative on 2 of 8 instruments**
  over the training window itself: GC=F 1,585 eval trades at 42% win rate,
  -118.4%; CL=F 1,330 trades at 51%, -128.3% (ES=F -7.4% despite a 60%
  win rate; YM=F the lone positive at +2.8%). The live desk never trades
  the raw signal without its filter stack, but the raw brain got WORSE
  where it traded most.
- **Timeframe mismatch risk**: states are shared across timeframes, so
  hourly-outcome re-weighting directly moves the Q-values the live 1m/5m
  desk reads. Negative transfer with zero demonstrated benefit fails the
  project's own evidence bar.

Action taken: Q-table restored from the pre-deep git snapshot, and the
5-minute practice mode (rough + trend windows -- the timeframe the desk
actually trades) re-run on the full 17-symbol watchlist, which also
finally gives the 9 forex crosses their first training reps. The
`--practice-deep` code stays in the repo for future re-evaluation (e.g.
if the state abstraction ever gains timeframe awareness), documented here
as tried-and-rejected, same treatment as scale-out exits (session 16).

## Session 33 (token-free self-correction inside the trading loop)

Directive: the bot must learn from its mistakes at "super speed" while
trading, autonomously -- no Claude, no tokens, no waiting for a human.
Inventory of what already self-corrects in-process on every closed trade:
online Q-learning (the outcome reweights the RL brain), setup grading
(`should_avoid` blocks setups with proven negative expectancy), the
mistakes log, anti-martingale sizing, and the loss-streak halt. Two gaps
remained, both standard desk discipline with documented failure modes:

1. **Anti-revenge cooldown** (`symbol_cooldown_minutes`, funded: 30).
   The desk cycles every minute; after a stop-out, the same signal that
   caused it usually still fires on the very next cycle -- the market
   condition that killed the trade hasn't gone anywhere in 60 seconds.
   Immediate re-entry after a loss is revenge trading with extra steps
   (documented as a top funded-account failure driver in the same breach
   research as session 31). Now: a losing close on a symbol blocks
   re-entry on THAT symbol for 30 minutes. Other symbols unaffected;
   manual mirrors exempt.

2. **Per-symbol probation** (`symbol_probation`, funded: on). The journal
   already graded setups but never graded WHERE the desk trades. Now any
   symbol whose own closed-trade record is net-negative over >=10 real
   trades is sized at HALF risk until its record recovers. Half-size
   (not a ban) deliberately: a banned symbol can never generate the
   trades that would clear its name -- probation keeps the sample
   growing at reduced cost. Self-updating from the journal on every
   close, in both directions.

Both are pure in-process rules reading data the desk already writes --
the "learns at super speed without tokens" property asked for: the lesson
is enforced on the very next 60-second cycle after the trade that taught
it.

Tests: `test_symbol_stats_and_minutes_since_last_loss`,
`test_cooldown_blocks_reentry_after_a_loss`,
`test_symbol_probation_halves_size_when_net_negative`.

100 tests passing.

## Session 34 (forensics: the take-profit had NEVER been hit -- RL exit made asymmetric)

Directive: another improvement pass, aimed at "stop playing scared, get
paid." Instead of guessing, pulled the exit reason for every one of the
22 real desk trades in the journal. The finding was structural, not
statistical noise:

- **0 of 22 trades ever reached the take-profit target.** The 1:2
  risk:reward payoff engine -- the entire reason the win rate only needs
  to clear ~33% -- had never engaged once.
- 10/22 were scratched by the RL "says exit" check before +1R, **7 of
  them while slightly green** (+$0.00 to +$0.51). The winners were being
  cut before they could become winners.
- 11/22 hit the 120-minute time stop (avg ~+$0.10 -- working as designed:
  scratch the thesis that never confirmed).
- 1/22 hit the actual stop-loss (-$2.26). The stop machinery works.

Fix: the pre-1R RL exit is now asymmetric -- it may only fire when the
position is RED (cutting a failing thesis early remains legitimate and
is standard practice), never on a green trade below +1R. Green trades
are left to resolve via target, stop, breakeven stop, or time stop.
Combined with session 26 (which protects breakeven-armed winners), the
RL exit now can only ever shrink losses, never truncate wins -- "cut
losses fast, let winners run," enforced mechanically.

Small sample caveat stated plainly: 22 trades is not statistical proof
of anything. But "the target has literally never been hit" is a
structural property of the exit logic, not a sample-size artifact, and
the change strictly matches the system's documented design intent.

Also re-ran the widened practice (4 rough + 4 trend windows, 30
episodes, full 17-symbol watchlist) as a further "past reps" pass on the
live Q-table.

Updated test: `test_rl_exit_still_works_before_breakeven_armed` now
asserts both directions (green pre-1R: hands off; red pre-1R: cut).

100 tests passing.

## Session 35 (widen the menu, not the risk: SILVER + US2000 + SOL weekend, ranked and switched every cycle)

Directive: "trade other things, find the best ones, always switch, don't
be fixed." The honesty-norm lever for "not making enough" is explicitly
"widen the candidate watchlist over loosening a risk filter" -- so that's
what this session does. The switching engine already exists
(`tradeability_score` re-ranks every candidate every cycle on trend
strength + movement potential + session liquidity, best setup gets first
claim on the risk budget); it just needed a bigger menu.

Added, with full plumbing (Yahoo aliases, TradeLocker names, spread
costs, correlation caps, all watchlists, preflight, practice/stress
watchlist):
- **SILVER (SI=F / XAGUSD)** -- deep, trends hard, but rides gold's macro
  drivers (~0.8 daily correlation), so it joins the metals correlation
  cluster: gold+silver together can never exceed the per-cluster cap and
  become one doubled metals bet. Spread modeled wider than gold (0.03%).
- **US2000 (RTY=F / Russell 2000)** -- the fourth US index future; joins
  the us-broad cluster with ES/YM. Spread modeled wider than ES/NQ.
- **SOL-USD** added to the weekend crypto fallback (paper account only;
  funded accounts stay weekend-off per Clarity rules) -- the #3 deepest
  24/7 book, same half-size weekend caution as BTC/ETH.

Verified real 5m data flows for all three before wiring (SI=F 1307 bars,
RTY=F 1304, SOL-USD 1394 over 5d). Watchlist: 17 -> 19 instruments.
Weekday shots on goal +2, weekend +1, all at the same risk bar -- more
places to find a trade, no lowering of what counts as one.

Tests: extended `test_index_alias_resolution` (aliases + spread ordering
SI>GC, RTY>ES) and the weekend-symbols default.

100 tests passing.

## Session 36 (video study: 4 full MambaFX-ecosystem transcripts, via the youtube-transcript skill)

Directive: find a way to "watch" trading videos and study hard. Installed
the `baoyu-youtube-transcript` skill (17.4K installs) and pulled FULL
transcripts of four videos -- one from MambaFX's own channel, two from a
former student (LumiixTrades) who now trades funded/prop rules, one from
a daily MambaFX-strategy live-trader (CAFX):
- MambaFX: "INSANE Scalping Strategy For SMALL Forex Accounts"
- LumiixTrades: "Mambafx Strategy Is Not What You Think" (Q&A breakdown)
- LumiixTrades: "I Mastered MambaFx Scalping Strategy And Made $4.1k"
- CAFX: "Easy Breakout Strategy | 1 Minute scalping | NAS100"

**What the transcripts independently confirm about our existing design
(strong validation, no changes needed):**
- The raw Mamba breakout chase produces "lots of fake outs, lots of
  losses" unless you swing his size -- his own former student says
  copying it under prop-firm rules doesn't work, and his fix is exactly
  our orb_retest_required: wait for the level to HOLD/retest, don't
  chase the break.
- CAFX live-states "my two rules: my two losses a day rule" -- the
  IDENTICAL number our max_consecutive_losses=2 uses.
- Both students move stops to breakeven quickly (our breakeven_at_1r),
  anchor direction and targets off higher-timeframe levels (our
  htf_confirm), and trade the NY open window hardest (our session
  weighting).
- MambaFX himself closes with: back test, demo trade for months first.
  The exact bar this project holds itself to.

**Genuinely new techniques observed -- each DEFERRED with reasons, not
rushed in the night before the funded handoff:**
1. *Fakeout-flip (stop-and-reverse)*: on a failed breakout, reverse into
   the opposite direction. Our desk is long-only; adding shorting is an
   architectural change needing its own research/tests. Real candidate,
   not a rush job.
2. *Aggressive trailing after +1R* (trail under successive higher lows)
   instead of a fixed 2R target -- how they turn 2R days into 5-7R days.
   Trailing-vs-fixed-target evidence is genuinely mixed (captures trend
   days, gives back chop days); session 34's asymmetric-exit fix must
   prove itself on real trades first before the exit machinery changes
   again.
3. *Structure-based targets at HTF levels* instead of mechanical 2R --
   needs S/R level detection; same deferral logic.

No risk parameters changed. Transcripts cached under youtube-transcript/
(gitignored). Study can continue with more videos on request -- the
pipeline is one command per video now.

## Session 37 (hybrid trail-after-target exit -- built, tested, deliberately OFF)

Followed up session 36's #1 deferred candidate with a real evidence pass
on trailing stops vs fixed targets:
- Comparative NQ backtest (Oct 2024-Jan 2025): fixed 20-tick trail PF
  1.1 / 42% WR; ATR 1.5x trail PF 1.6 / 48% WR; structure trail PF 1.8 /
  51% WR (proptradingvibes writeup). ATR-based trailing meaningfully
  beats naive fixed-distance trailing.
- Long-running systematic trend-following research cuts the other way:
  adding trailing stops REDUCED total return in most tests, because the
  top 10-15% of trades produce nearly all profit and trails truncate
  exactly those. Evidence is genuinely two-sided.
- The reconciling pattern (multiple practitioner sources + both video
  traders from session 36): HYBRID -- fixed stop/target discipline until
  the trade reaches the original target, THEN convert to a trail so only
  already-won trades can run. Floor at +1R so a converted trade can
  never give back more than the last R.

Implemented exactly that shape: `DeskConfig.trail_after_target` -- at
the fixed target, instead of cashing out, the trade records a high-water
mark (persisted in the journal record's tags, survives restarts) and
exits when price falls one stop-distance off that mark, floored at
target - 1R. **Default OFF everywhere, including funded config**: the
session 34 asymmetric-exit fix changes the same machinery and must show
its effect on real trades first -- two overlapping exit changes at once
would make the journal unreadable as evidence. Flip criterion: after
~30 real trades under session 34's exit, if winners are reaching the 2R
target consistently, turn this on and compare the next 30.

Tests: `test_fixed_target_still_exits_by_default`,
`test_trail_after_target_lets_winners_run_then_locks_gains`.

102 tests passing.

## Session 38 (the cash-out playbook: how profits actually become money at Clarity)

Studied the part nobody had researched yet: the payout pipeline. Facts
gathered (their site + review aggregators; numbers conflict between
sources in places, so VERIFY IN THE DASHBOARD once the accounts exist):

- **Cadence**: Instant accounts allow payout requests every 14 days,
  starting 14 days after the first trade. Some plans also carry a
  minimum-trading-days requirement (sources quote 1-5 days depending on
  plan) -- a day with zero trades does NOT count as a trading day.
- **Minimum payout $100**, requested from the account dashboard.
  Payouts are made in USDT (crypto) -- the user needs a crypto wallet
  address ready or the money has nowhere to land.
- **Profit split**: marketed "up to 90-100%" depending on plan/add-ons;
  the real number for our specific accounts must be read off the
  purchase page at buy time.
- **Consistency rule decides payout eligibility, not just termination**:
  their FAQ example says no single day may exceed ~10% of a requested
  payout on Instant accounts (review sites quote a 30% rule for other
  account types -- another number to verify at purchase). Concentrated
  profits = delayed payouts.

**Why the desk's shape already matches the payout rules:** the 3% daily
profit target caps day size, the daily loss guard caps day damage, and
steady multi-day grinding is exactly what a "no single day too big"
eligibility rule pays. One monster day would literally be less
withdrawable than five modest ones. Also noted: if a payout clock needs
trading days, long no-trade streaks (e.g. every filter red for days)
would slow the calendar -- worth watching in the journal, NOT worth
loosening filters over.

**Purchase-day checklist (consolidated from sessions 30-38):**
1. Buy both accounts WITH the "EA's Allowed" add-on (bots banned
   without it). Skip "Trade on Weekends" unless weekend crypto is
   wanted -- the funded bots are configured weekend-off either way.
2. Note each account's exact profit split + consistency numbers from
   the purchase page.
3. Have a USDT wallet address ready for payouts.
4. Send each account's TradeLocker email/password/server -> preflight
   -> demo-first -> `bash scripts/run_funded_accounts.sh`.
