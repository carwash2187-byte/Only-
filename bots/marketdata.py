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
