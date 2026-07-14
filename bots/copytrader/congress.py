"""Follow US Congress stock trades (STOCK Act public disclosures).

Best-effort module: the free mirrors of congressional trade data come and go
(the community Stock Watcher S3 buckets went dark, Capitol Trades' unofficial
API rate-limits aggressively). Each source below is tried in order and any
failure degrades gracefully to the next; if all are down this returns [] and
the trading desk simply runs on the (reliable, official) SEC 13F whale signal.

Disclosures can legally arrive up to 45 days after the trade, so this is a
"what smart-connected money bought recently" signal, not a same-day mirror.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

import requests

TIMEOUT = 60
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


@dataclass
class CongressTrade:
    politician: str
    ticker: str
    transaction: str  # "purchase" or "sale"
    amount: str
    date: str
    chamber: str


def _parse_date(raw: str) -> Optional[datetime]:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip()[:10], fmt)
        except (ValueError, AttributeError):
            continue
    return None


# -- source 1: Capitol Trades (unofficial API of capitoltrades.com) -----------

def _fetch_capitoltrades(days: int) -> List[CongressTrade]:
    trades: List[CongressTrade] = []
    for page in range(1, 4):
        resp = requests.get(
            "https://bff.capitoltrades.com/trades",
            params={"pageSize": 100, "page": page, "txDate": f"{days}d"},
            headers=BROWSER_UA,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            break
        for item in data:
            issuer = item.get("issuer") or {}
            politician = item.get("politician") or {}
            ticker = (issuer.get("issuerTicker") or "").split(":")[0].strip().upper()
            if not ticker:
                continue
            trades.append(
                CongressTrade(
                    politician=f"{politician.get('firstName', '')} {politician.get('lastName', '')}".strip(),
                    ticker=ticker,
                    transaction=(item.get("txType") or "").lower(),
                    amount=str(item.get("value") or ""),
                    date=str(item.get("txDate") or ""),
                    chamber=(politician.get("chamber") or "unknown"),
                )
            )
    return trades


# -- source 2/3: community Stock Watcher S3 mirrors (historically great, now
#    intermittent; kept because they may work from your network) --------------

_STOCK_WATCHER_FEEDS = (
    (
        "senate",
        "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json",
        "senator",
    ),
    (
        "house",
        "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
        "representative",
    ),
)


def _fetch_stock_watcher(days: int) -> List[CongressTrade]:
    trades: List[CongressTrade] = []
    cutoff = datetime.now() - timedelta(days=days)
    for chamber, url, name_key in _STOCK_WATCHER_FEEDS:
        resp = requests.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        for item in resp.json():
            date = _parse_date(item.get("transaction_date", ""))
            if date is None or date < cutoff:
                continue
            ticker = (item.get("ticker") or "").strip().upper()
            if not ticker or ticker in ("--", "N/A"):
                continue
            trades.append(
                CongressTrade(
                    politician=item.get(name_key, "unknown"),
                    ticker=ticker,
                    transaction=(item.get("type") or "").lower(),
                    amount=item.get("amount", ""),
                    date=date.strftime("%Y-%m-%d"),
                    chamber=chamber,
                )
            )
    return trades


def recent_trades(days: int = 90, purchases_only: bool = True) -> List[CongressTrade]:
    """Congress trades disclosed in the last `days` days, newest first.

    Returns [] when every data source is unreachable.
    """
    trades: List[CongressTrade] = []
    for name, fetcher in (("capitoltrades", _fetch_capitoltrades),
                          ("stock-watcher", _fetch_stock_watcher)):
        try:
            trades = fetcher(days)
            if trades:
                break
        except (requests.RequestException, ValueError, KeyError) as exc:
            print(f"[congress] {name} source unavailable: {exc}")
    if purchases_only:
        trades = [t for t in trades if "purchase" in t.transaction or t.transaction == "buy"]
    trades.sort(key=lambda t: t.date, reverse=True)
    return trades
