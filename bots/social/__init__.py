"""Watch YouTube, Twitter/X, and Instagram for trade calls from people you
follow, and auto-queue anything that looks like a call via the same manual
mirror pipeline (bots.copytrader.manual) already graded by the journal.

Reliability is NOT equal across sources -- be aware before relying on this:

- bots.social.youtube: uses the public per-channel RSS feed (no API key) +
  youtube-transcript-api for caption text. Stable, official-ish, low risk.
- bots.social.twitter: uses twscrape against X's internal API with a real
  logged-in account. Against X's Terms of Service; accounts used for this
  get rate-limited or banned periodically. Use a throwaway account, never
  your main one.
- bots.social.instagram: uses instaloader. Instagram actively fights
  scrapers (checkpoints, rate limits), especially for Stories, which are
  not part of any public API at all. Same rule: throwaway account only.

Call extraction (bots.social.signals) is regex-based pattern matching
("BUY AAPL", "long EURUSD", "$TSLA short") on caption/tweet/transcript text
-- it catches explicit calls, not chart screenshots or implied sentiment.
"""

from bots.social.signals import extract_calls

__all__ = ["extract_calls"]
