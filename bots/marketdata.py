"""Market data access for the bot stack.

Primary source is Yahoo Finance's chart API via plain `requests` (works
through restrictive proxies where yfinance's curl_cffi backend gets reset);
yfinance is kept as a fallback. Forex pairs written as EURUSD / EUR_USD are
automatically tried as Yahoo's EURUSD=X form.
"""

from __future__ import annotations

from typing import List, Optional

import pandas as pd
import requests

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 30

# Broker-style CFD/index nicknames (what MambaFX and most retail platforms
# actually call these -- "US30", "NAS100") mapped to their real, liquid
# Yahoo futures tickers. Futures trade nearly 24/5 like forex (unlike the
# ^DJI/^NDX cash index, which only updates during NYSE hours), which is
# what makes them tradeable on the same round-the-clock schedule as the
# forex desk instead of being locked to the 9:30-16:00 stock session.
INDEX_ALIASES = {
    "US30": "YM=F", "DOW": "YM=F", "DOW30": "YM=F", "DJ30": "YM=F",
    "NAS100": "NQ=F", "NASDAQ": "NQ=F", "NASDAQ100": "NQ=F", "NDX": "NQ=F",
    "US500": "ES=F", "SPX": "ES=F", "SPX500": "ES=F", "SP500": "ES=F",
}


def resolve_symbol(symbol: str) -> str:
    """Map a friendly index/CFD nickname to its real tradeable ticker.
    Passes through unchanged if it isn't a known alias (forex pairs,
    stocks, and already-correct tickers like 'YM=F' are untouched)."""
    return INDEX_ALIASES.get(symbol.strip().upper(), symbol)


def _candidates(symbol: str) -> List[str]:
    symbol = symbol.strip()
    compact = symbol.upper().replace("_", "").replace("/", "")
    out: List[str] = []
    if len(compact) == 6 and compact.isalpha() and not symbol.upper().endswith("=X"):
        # looks like a forex pair: try Yahoo's EURUSD=X form first
        out.append(f"{compact}=X")
    out.append(symbol)
    return out


def _fetch_chart(symbol: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    try:
        resp = requests.get(
            CHART_URL.format(symbol=symbol),
            params={"range": period, "interval": interval},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        result = (resp.json().get("chart", {}).get("result") or [None])[0]
        if not result:
            return None
        quote = result["indicators"]["quote"][0]
        df = pd.DataFrame(
            {
                "open": quote.get("open"),
                "high": quote.get("high"),
                "low": quote.get("low"),
                "close": quote.get("close"),
                "volume": quote.get("volume"),
            },
            index=pd.to_datetime(result.get("timestamp", []), unit="s"),
        )
        return df.dropna(subset=["close"])
    except Exception:
        return None


def _fetch_yfinance(symbol: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf

        df = yf.Ticker(symbol).history(period=period, interval=interval)
        return df if df is not None and not df.empty else None
    except Exception:
        return None


def get_history(symbol: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """Daily (or intraday) OHLCV history with a lowercase `close` column."""
    for candidate in _candidates(symbol):
        df = _fetch_chart(candidate, period, interval)
        if df is None:
            df = _fetch_yfinance(candidate, period, interval)
        if df is not None and not df.empty:
            return df
    raise ValueError(f"No market data available for {symbol}")


def get_price(symbol: str) -> float:
    """Latest available close price."""
    df = get_history(symbol, period="5d", interval="1d")
    return float(df["close"].iloc[-1] if "close" in df.columns else df["Close"].iloc[-1])
