"""Trading bot stack built on top of TradingAgents.

Modules:
- bots.journal        -- trade journal + learn-from-mistakes memory
- bots.learning       -- reinforcement-learning trading agent (Q-learning core,
                         plus a ready-to-run freqtrade/FreqAI setup)
- bots.copytrader     -- follows top investors (SEC 13F whales, Congress trades)
- bots.brokers        -- execution: paper (default), Alpaca, Robinhood, crypto
- bots.organization   -- ties everything together like a trading firm's desks

Nothing in this package guarantees profit. Paper trading is the default
everywhere; live trading requires explicit configuration and your own API keys.
"""

__version__ = "0.1.0"
