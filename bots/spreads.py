"""Approximate real-world bid/ask spread costs, as a fraction of price, by
instrument category (session 27).

Every fill in PaperBroker was happening at the exact quoted mid-price --
free trading, which no real account gets. This isn't meant to be an exact
quote from any specific broker (spreads vary by broker, liquidity, and
time of day); it's an honest approximation so the paper account's numbers
stop being systematically more optimistic than a funded account would be,
sourced from typical retail/institutional spread data:
- forex majors (EURUSD/GBPUSD/USDJPY): ~0.5-1.5 pips
- index futures (ES/NQ/YM): 1-2 ticks, very tight relative to price
- gold/oil futures: 1-5 ticks, wider relative to price than the indices
- crypto: 3-5x wider than forex majors even on weekdays (session 8),
  measurably worse again on weekends since spot ETFs concentrated
  market-making into weekday hours (session 19)
"""

from __future__ import annotations

_FOREX_MAJOR = {"EURUSD", "GBPUSD", "USDJPY"}
_JPY_CROSS = {"EURJPY", "GBPJPY", "AUDJPY"}
_FOREX_CROSS = {"AUDUSD", "NZDUSD", "USDCHF", "USDCAD", "EURGBP", "EURCHF"}


def spread_pct(symbol: str, now=None) -> float:
    """Round-trip-relevant half-spread is spread_pct/2 on each side;
    buy() and sell() apply half on entry and half on exit so a full
    round trip costs the whole spread_pct."""
    compact = symbol.upper().replace("_", "").replace("/", "")
    if compact in _FOREX_MAJOR:
        return 0.0001  # ~1 pip
    if compact in _JPY_CROSS:
        return 0.0002
    if compact in _FOREX_CROSS:
        return 0.00015
    if compact == "NQ=F":
        return 0.00002
    if compact in ("ES=F", "YM=F"):
        return 0.00004
    if compact == "GC=F":
        return 0.0001
    if compact == "CL=F":
        return 0.0003
    if compact.endswith(("-USD", "-USDT")):
        from bots.organization import is_weekend_forex_gap

        base = 0.0005
        if is_weekend_forex_gap(now):
            base *= 2.0  # session 19: weekend crypto liquidity measurably worse
        return base
    return 0.00005  # stocks: tight, liquid large-cap names
