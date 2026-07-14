"""Copy trading: follow investors with a public, verifiable track record.

Instead of scraping fragile exchange leaderboards (which break constantly and
are full of pump-and-dump accounts), this module follows two official/public
data sources:

- SEC EDGAR 13F filings (bots.copytrader.sec_13f): what famous fund managers
  (Buffett, Burry, Ackman, ...) actually hold. Free, official SEC API.
- US Congress trade disclosures (bots.copytrader.congress): stock trades
  reported by members of Congress. Public STOCK Act data.

bots.copytrader.signals merges both into ranked consensus buy signals.

Reality check: 13Fs are published up to 45 days after quarter end and Congress
disclosures up to 45 days after the trade -- you are copying with a delay, and
none of this guarantees profit.
"""

from bots.copytrader.signals import CopySignal, consensus_signals

__all__ = ["CopySignal", "consensus_signals"]
