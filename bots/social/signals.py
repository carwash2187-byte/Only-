"""Extract trade calls from free-form text (captions, tweets, transcripts).

Regex pattern matching, not NLP -- catches explicit calls like "BUY AAPL",
"long EURUSD", "$TSLA short", not chart screenshots or implied sentiment.
False negatives (missing a real call) are far more likely than false
positives, by design: a missed call costs nothing, a wrong auto-trade costs
real risk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

# Common words that match the ticker pattern but are never tickers, so they
# don't get misread as symbols ("I" as a sentence, "GO" as a filler word...).
_STOPWORDS = {
    "I", "A", "THE", "TO", "IS", "IN", "ON", "AT", "IT", "GO", "UP", "DOWN",
    "ALL", "NOW", "OUT", "FOR", "ARE", "AND", "BUT", "NOT", "YOU", "PDF",
    "USA", "USD", "CEO", "IPO", "ATH", "ATL", "TP", "SL",
}

_BUY_WORDS = r"(?:BUY|LONG|LONGING|GOING LONG)"
_SELL_WORDS = r"(?:SELL|SHORT|SHORTING|GOING SHORT)"
# The symbol group deliberately stays case-SENSITIVE (real tickers are
# written in caps); only the keyword is matched case-insensitively via a
# scoped inline flag, or "Just went long" would misread "Just" as a ticker.
_SYMBOL = r"\$?([A-Z]{2,6}(?:USD|USDT|JPY|GBP|EUR)?)\b"

_PATTERNS = [
    (re.compile(rf"(?i:\b{_BUY_WORDS}\b)[^.\n]{{0,15}}?{_SYMBOL}"), "buy"),
    (re.compile(rf"{_SYMBOL}[^.\n]{{0,15}}?(?i:\b{_BUY_WORDS}\b)"), "buy"),
    (re.compile(rf"(?i:\b{_SELL_WORDS}\b)[^.\n]{{0,15}}?{_SYMBOL}"), "sell"),
    (re.compile(rf"{_SYMBOL}[^.\n]{{0,15}}?(?i:\b{_SELL_WORDS}\b)"), "sell"),
]


@dataclass
class DetectedCall:
    symbol: str
    side: str  # "buy" or "sell"
    snippet: str


def extract_calls(text: str) -> List[DetectedCall]:
    if not text:
        return []
    calls: List[DetectedCall] = []
    seen = set()
    for pattern, side in _PATTERNS:
        for match in pattern.finditer(text):
            symbol = match.group(1).upper()
            if symbol in _STOPWORDS:
                continue
            key = (symbol, side)
            if key in seen:
                continue
            seen.add(key)
            start = max(0, match.start() - 20)
            end = min(len(text), match.end() + 20)
            snippet = text[start:end].strip().replace("\n", " ")
            calls.append(DetectedCall(symbol=symbol, side=side, snippet=snippet))
    return calls
