# AI Trading Bot Research — GitHub Projects

Research date: 2026-07-14

Two asks were researched: (1) trading bots that **learn from their mistakes**, and
(2) bots that **copy profitable traders**. Findings below, with an honest note up
front: **no bot on GitHub (or anywhere) makes guaranteed money.** Any project,
service, or person promising guaranteed returns is a scam signal, not a feature.
Copy trading also does not guarantee profit — you get worse fills than the trader
you copy (latency + slippage), and leaderboard "winners" frequently blow up.

## 0. What this repo already is

This repository is a copy of
[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) —
a multi-agent LLM trading framework. Notably, it **already has a
learn-from-mistakes mechanism**: its agents keep a reflection memory of past
decisions and outcomes and feed those reflections into future decisions. So one
of the things being searched for is already installed here.

## 1. Bots that learn from mistakes (machine learning / reinforcement learning)

| Project | Link | What it is |
|---|---|---|
| **Freqtrade + FreqAI** | [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) | The most popular open-source crypto bot. Its built-in [FreqAI](https://www.freqtrade.io/en/stable/freqai/) module continuously **retrains ML models during live trading** to adapt to the market, and has a [reinforcement-learning mode](https://www.freqtrade.io/en/stable/freqai-reinforcement-learning/) where the agent is rewarded/punished based on trade outcomes — the literal "learn from mistakes" loop. Has dry-run (paper trading) mode. Actively maintained. |
| **FinRL** | [AI4Finance-Foundation/FinRL](https://github.com/AI4Finance-Foundation/FinRL) | ~15.7k stars, actively maintained (v0.3.8, Mar 2026). Financial reinforcement-learning framework: agents (PPO, SAC, TD3, A2C, DDPG) learn trading policies by trial and error in market environments. Research/education oriented; the team points production users to FinRL-Trading. |
| **TensorTrade** | [tensortrade-org/tensortrade](https://github.com/tensortrade-org/tensortrade) | Framework for building RL trading agents. Well-known but less actively developed now. |
| **OctoBot** | [Drakkar-Software/OctoBot](https://github.com/Drakkar-Software/OctoBot) | Free open-source bot with AI, grid, DCA and TradingView strategies on 15+ exchanges. Easiest UI of the group. |
| **Deep Q-learning stock bot** | [pskrunner14/trading-bot](https://github.com/pskrunner14/trading-bot) | Small educational Deep Q-Learning bot — good for understanding how RL "learning from mistakes" actually works. |
| **RL Bitcoin bot tutorial series** | [pythonlessons/RL-Bitcoin-trading-bot](https://github.com/pythonlessons/RL-Bitcoin-trading-bot) | Step-by-step tutorial building an RL Bitcoin bot from scratch. |
| **DRL bots (educational)** | [RohanSreelesh/Reinforcement-learning-based-trading-bot](https://github.com/RohanSreelesh/Reinforcement-learning-based-trading-bot), [nicoDs96/Trading-Bot---Deep-Reinforcement-Learning](https://github.com/nicoDs96/Trading-Bot---Deep-Reinforcement-Learning) | DQN/PPO experiments on stocks; useful to study, not production systems. |

**Best starting point:** Freqtrade with FreqAI. It is the most battle-tested, has
real documentation, a big community, and a dry-run mode so it can be run for
weeks with zero money at risk before going live.

## 2. Bots that copy profitable traders

| Project | Link | What it is |
|---|---|---|
| **Binance leaderboard copy bot** | [tpmmthomas/binance-copy-trade-bot](https://github.com/tpmmthomas/binance-copy-trade-bot) | Verified real (~144 stars). Watches traders on the Binance Futures **leaderboard** and mirrors their positions into a Bybit account; Telegram/Discord notifications. Last confirmed working Feb 2024 — expect maintenance work, since Binance changes its leaderboard pages. |
| **Whale wallet mirror (Solana/Base)** | [Rezzecup/whale-wallet-mirror-copy-trader](https://github.com/Rezzecup/whale-wallet-mirror-copy-trader) | On-chain copy trading: watches "smart money" wallets, scores them 0–100 by win rate and drawdown, and mirrors their swaps with position sizing/slippage limits. Has a paper mode (`--mode paper`) that simulates without spending. |
| **Solana copy trader** | [tumf/solana-copy-trader](https://github.com/tumf/solana-copy-trader) | Simpler Solana wallet-mirroring bot. |
| **Wallet monitor + alerts** | [rdin777/solana-copy-trade-bot-public](https://github.com/rdin777/solana-copy-trade-bot-public) | Watches a list of wallets and sends Telegram alerts on large trades — copy manually instead of automatically. Lower risk way to start. |
| **Browse more** | [github.com/topics/copy-trade](https://github.com/topics/copy-trade) | GitHub's copy-trade topic, sortable by stars/recent activity. |

### ⚠️ Copy-trading repos are a scam minefield

The crypto copy-bot corner of GitHub is heavily polluted with fake repos that are
actually **wallet drainers** or lures to paid Telegram "services." Red flags:

- README is all marketing hype ("guaranteed profits", "zero-loss") with little code
- Asks you to fund a wallet, paste a private key/seed phrase, or DM on Telegram
- Fresh account, few real commits, stars that appeared all at once
- Pre-built binaries instead of readable source

Rules regardless of which repo is used: **read the code before running it**, run in
paper/dry-run mode first, use exchange API keys **with withdrawal permission
disabled**, and on-chain use a burner wallet holding only what you can lose.

## 3. Reality check on "guaranteed money"

- Backtest profits usually vanish live (overfitting, fees, slippage, latency).
- Leaderboard traders are often survivors of luck; many blow up after you start
  copying them, and some run pump-and-dump schemes that profit *from* copiers.
- RL/ML bots adapt statistically — they do not "understand" markets, and most
  retail bots lose money after costs.
- The only safe workflow: paper trade for weeks → tiny real capital → scale only
  what survives. Anything sold as guaranteed is a scam.
