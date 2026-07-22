# Trading Bot Stack

Four bots working together like a small trading firm, built on top of the
TradingAgents framework already in this repo.

**No bot guarantees profit.** Everything here defaults to paper (fake money)
trading. Live trading requires your own API keys and explicit opt-in flags.

## Quick start

```bash
pip install -e .            # installs this repo (pandas, yfinance, etc.)
python -m bots demo         # safe end-to-end demo, no internet or money needed
python -m bots train --symbol SPY --period 2y    # train the RL agent
python -m bots signals      # what the smart money is buying right now
python -m bots trade        # one trading-desk cycle on the paper account
python -m bots journal      # results + lessons learned so far
python -m bots autopilot    # hands-free: full desk cycle every 30 min
```

### Day trading vs swing trading

By default the desk looks at daily candles and holds positions across days
(swing trading). For day trading -- many trades a session, everything closed
before the close -- pass `--day-trading`:

```bash
python -m bots autopilot --broker paper --day-trading --interval 5
```

This switches signals to 5-minute candles and, in the last 15 minutes before
the 4pm ET close, automatically sells every open position instead of running
a normal cycle (`TradingDesk.flatten_all()`) -- real day traders don't hold
overnight, since the stop-loss/take-profit checks only run while the desk is
awake and can't react to an overnight gap. Use `--timeframe 15m` / `1h` etc.
to pick a different candle size.

### Running it yourself, all day, for free

`python -m bots autopilot` is plain Python in a loop -- no LLM calls, no
Claude usage, just broker + market-data API calls on a timer. Run it on any
computer that stays on (your laptop, a $5/mo VPS, a Raspberry Pi) and it
trades completely on its own for as long as that machine is up, at zero
ongoing cost beyond electricity. Close the terminal and it stops; all state
(journal, trained model, day baseline) is saved after every action, so
starting it again later picks up exactly where it left off.

### Hands-free mode

`python -m bots autopilot --broker alpaca` keeps running desk cycles on its
own: it respects market hours (stocks 9:30-16:00 ET, forex 24/5, crypto 24/7),
applies the daily 5% circuit breaker and per-trade risk caps every cycle, and
journals everything. Leave it running on any computer that stays on (an old
laptop, a $5 VPS, a Raspberry Pi). Check `python -m bots journal` weekly --
autopilot automates the discipline, not the profit.

## What each piece does

### 1. `bots/journal.py` — the memory that learns from mistakes
Every trade is recorded with the "setup" that caused it. Once a setup has
enough history and a losing average, the desk **refuses to take that setup
again** until it improves. Also counts day trades for the PDT rule (below).

### 2. `bots/learning/` — the bot that learns by trial and error
A reinforcement-learning agent (Q-learning) walks through price history making
buy/sell/hold decisions. Losing decisions lower the value of repeating that
action in that market state; winning ones raise it. Train it on any symbol
with `python -m bots train --symbol AAPL`.

For the production-grade version of the same idea, `bots/learning/freqai/`
contains a ready-to-run config + strategy for
[freqtrade](https://github.com/freqtrade/freqtrade)'s FreqAI — a crypto bot
whose ML model **retrains itself every few hours** on fresh market data.
Install with `bash scripts/install_trading_stack.sh`.

**Practice on synthetic regimes** — `python -m bots practice --scenarios 300`
drills the agent across hundreds of manufactured market regimes it rarely
sees in the thin real history (flash crashes, news whipsaws, gap opens,
blow-off tops, grinding chop) and prints a per-regime win-rate/return
breakdown so you can see *where the policy bleeds*. Report-only by default;
`--save` hardens the live Q-table in place. Synthetic P&L is not a track
record — it's practice and a stress test, not a profit claim.

### 3. `bots/copytrader/` — copy people with a public track record
- **SEC 13F filings** (official, free): pulls the latest reported holdings of
  Warren Buffett (Berkshire), Michael Burry (Scion), Bill Ackman (Pershing
  Square), Stanley Druckenmiller (Duquesne), and Bridgewater, straight from
  SEC EDGAR. Set `SEC_EDGAR_UA="YourName your@email.com"` (the SEC requires a
  contact in the User-Agent).
- **Congress trades** (STOCK Act disclosures): best-effort, tries several free
  mirrors and skips the signal if they're down.
- `consensus_signals()` ranks tickers by how many independent smart-money
  sources hold or just bought them.

Know the delay: 13Fs appear up to 45 days after quarter end, Congress trades
up to 45 days after execution. You are copying positions, not ticks — and past
performance still doesn't guarantee anything.

### 4. `bots/brokers/` — where orders go
| Broker | Command | Notes |
|---|---|---|
| Paper (default) | `python -m bots trade` | Local simulation, zero risk. |
| **Alpaca** (recommended) | `python -m bots trade --broker alpaca` | Official free API for US stocks with a built-in fake-money mode. Sign up at alpaca.markets, set `ALPACA_API_KEY_ID` + `ALPACA_API_SECRET_KEY`. Stays in paper mode until you set `ALPACA_LIVE=1`. |
| Robinhood | `--broker robinhood` | **Unofficial** (via [robin_stocks](https://github.com/jmfernandes/robin_stocks)) — Robinhood has no public API. Can break anytime, may violate their terms, has NO paper mode (all orders are real money). Set `ROBINHOOD_USERNAME`/`ROBINHOOD_PASSWORD`. Requires the `--live-i-understand-the-risk` flag. |
| Crypto | `--broker crypto` | 100+ exchanges via [ccxt](https://github.com/ccxt/ccxt). Sandbox/testnet mode by default. Set `CRYPTO_EXCHANGE`, `CRYPTO_API_KEY`, `CRYPTO_API_SECRET`. |
| **OANDA** (forex) | `--broker oanda` | Official free forex API with practice accounts (fake money, real prices) — the right place to try scalping styles. Set `OANDA_API_TOKEN` + `OANDA_ACCOUNT_ID`; stays on the practice server until `OANDA_LIVE=1`. |
| **TradeLocker** (prop firms) | `--broker tradelocker` | The platform many prop firms use, via its official Python API (`pip install tradelocker`). Demo environment by default; set `TRADELOCKER_EMAIL`/`TRADELOCKER_PASSWORD`/`TRADELOCKER_SERVER`, and `TRADELOCKER_LIVE=1` only for real accounts. **Check your prop firm's automation rules first** — bots are usually allowed, cross-account copy trading often isn't, and violations forfeit funded accounts. |
| **MetaTrader 5** | `--broker mt5` | Official MetaTrader5 Python package. **Windows only**, and the MT5 terminal must be installed, running, logged in (demo account first!), with "Allow algorithmic trading" enabled. Refuses real accounts unless `MT5_LIVE=1`. |

### 5. `bots/organization.py` — the trading firm
One `run_once()` cycle works like a real desk:
1. **Research desk** proposes candidates from the copy-trading consensus.
2. **Quant desk** (the RL agent) votes on each one; bearish = skip.
3. **AI committee** (optional, `--llm-committee`): the TradingAgents
   analyst/researcher/risk-debate graph in this repo rates the ticker
   Buy/Hold/Sell (needs LLM API keys, costs money per call).
4. **Risk desk** sizes positions (max 15% of equity each, max 5 positions),
   enforces stop-loss (-5%) / take-profit (+15%), blocks setups the journal
   says lose money, and applies the PDT guard.
5. **Execution desk** sends orders to your chosen broker and journals them.

## Copying a human trader you follow (Instagram / YouTube / Discord)

There is no API for an influencer's Instagram stories, and win-rate claims
("he only loses 5% of trades") from social media are unverifiable marketing —
many influencer traders earn from courses and signal groups, not from trading.
So the mirror mode works the honest way around:

```bash
python -m bots mirror EURUSD --side buy --source mambafx --note "IG story"
python -m bots trade --broker oanda     # executes it on a practice account
python -m bots journal                  # shows mirror:mambafx real performance
```

Three protections apply automatically to every mirrored call:
1. **Risk-per-trade cap** — position sized so a stopped-out trade costs at most
   1% of the account (`risk_per_trade_pct`).
2. **Daily circuit breaker** — if the account is down 5% on the day, the desk
   stops opening trades until tomorrow (`max_daily_loss_pct`). Code-enforced
   discipline, no willpower needed.
3. **Source grading** — the journal tracks each source's real results in YOUR
   account. If `mirror:someguy` averages a loss after 5+ trades, the desk
   auto-blocks that source. You find out for real whether the influencer's
   calls make money.

## Where the bot's memory lives

All state (trade journal, trained Q-table, paper account, day baseline) sits in
the directory named by `BOT_DATA_DIR` (default `bot_data/`, git-ignored). The
committed `paper_state/` directory is the shared cloud paper-trading account:
run with `BOT_DATA_DIR=paper_state` and commit the changes so the track record
and everything the bot has learned survive across machines and sessions.

## Day-trading reality check (US stocks)

The **pattern day trader (PDT) rule**: with less than $25,000 of equity in a
margin account you're limited to 3 day trades per 5 business days — brokers
(including Robinhood and Alpaca) will restrict the account beyond that. The
desk has a built-in guard for live accounts. Also: RL and copy signals here
are built on daily data — this stack is a swing-trading assistant, not a
high-frequency day-trading engine.

## The bigger open-source engines this builds on

Installed by `scripts/install_trading_stack.sh`:
- [freqtrade](https://github.com/freqtrade/freqtrade) + FreqAI — self-retraining crypto trading (config included here)
- [FinRL](https://github.com/AI4Finance-Foundation/FinRL) — deep-RL research framework (`--with-finrl`)
- [ccxt](https://github.com/ccxt/ccxt) — exchange connectivity
- [robin_stocks](https://github.com/jmfernandes/robin_stocks) — unofficial Robinhood API

And the LLM "organization" itself:
[TradingAgents](https://github.com/TauricResearch/TradingAgents) — this repo.
