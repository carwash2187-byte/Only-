"""Merge whale 13F holdings and Congress purchases into consensus buy signals.

A ticker scores points for every independent "smart money" source that holds
or recently bought it. More independent sources agreeing = higher score. The
trading desk takes the top-ranked names as buy candidates.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List

from bots.copytrader import congress, sec_13f


@dataclass
class CopySignal:
    symbol: str
    score: float
    sources: List[str] = field(default_factory=list)

    def describe(self) -> str:
        return f"{self.symbol} (score {self.score:.1f}): " + "; ".join(self.sources)


def consensus_signals(
    top_n_holdings: int = 10,
    congress_days: int = 90,
    include_13f: bool = True,
    include_congress: bool = True,
    max_signals: int = 15,
) -> List[CopySignal]:
    scores: Dict[str, float] = defaultdict(float)
    sources: Dict[str, List[str]] = defaultdict(list)

    if include_13f:
        for manager, holdings in sec_13f.top_holdings(top_n=top_n_holdings).items():
            for rank, holding in enumerate(holdings):
                if not holding.ticker:
                    continue
                # A #1 position is a stronger conviction signal than a #10.
                weight = 1.0 + (top_n_holdings - rank) / top_n_holdings
                scores[holding.ticker] += weight
                sources[holding.ticker].append(f"{manager} top-{rank + 1} holding")

    if include_congress:
        buyers: Dict[str, set] = defaultdict(set)
        for trade in congress.recent_trades(days=congress_days, purchases_only=True):
            buyers[trade.ticker].add(trade.politician)
        for ticker, politicians in buyers.items():
            # 0.5 points per distinct politician buying, capped so Congress
            # noise can't drown out fund-manager conviction.
            scores[ticker] += min(len(politicians) * 0.5, 3.0)
            sources[ticker].append(f"{len(politicians)} Congress member(s) bought recently")

    signals = [
        CopySignal(symbol=symbol, score=score, sources=sources[symbol])
        for symbol, score in scores.items()
    ]
    signals.sort(key=lambda s: s.score, reverse=True)
    return signals[:max_signals]


if __name__ == "__main__":
    for signal in consensus_signals():
        print(signal.describe())
