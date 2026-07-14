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
