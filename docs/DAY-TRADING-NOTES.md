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
