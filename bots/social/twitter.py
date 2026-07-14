"""Watch an X/Twitter account for tweets that look like trade calls.

Uses twscrape (github.com/vladkens/twscrape) against X's internal API. This
is NOT an official API and is against X's Terms of Service -- accounts used
for this get rate-limited or suspended periodically, unpredictably. Use a
throwaway X account you don't care about losing, never a real one.

Install: pip install twscrape
Setup (one-time, from a shell):
    twscrape add_accounts accounts.txt username:password:email:email_password
    twscrape login_all
Then set TWSCRAPE_DB=accounts.db (default) and this module reuses the pool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class Tweet:
    id: str
    text: str
    created_at: str


async def _fetch_recent(username: str, limit: int) -> List[Tweet]:
    from twscrape import API

    api = API()
    user = await api.user_by_login(username)
    if user is None:
        raise ValueError(f"X user @{username} not found (or account pool has no valid login)")
    tweets = []
    async for tweet in api.user_tweets(user.id, limit=limit):
        tweets.append(Tweet(id=str(tweet.id), text=tweet.rawContent, created_at=str(tweet.date)))
    return tweets


def recent_tweets(username: str, limit: int = 10) -> List[Tweet]:
    try:
        import twscrape  # noqa: F401
    except ImportError as exc:
        raise ImportError("twscrape is not installed. Run: pip install twscrape") from exc
    import asyncio

    return asyncio.run(_fetch_recent(username.lstrip("@"), limit))
