"""News blackout guard: don't open trades right around high-impact news.

Study findings this implements (docs/DAY-TRADING-NOTES.md session 3):
- 13:30 UTC / 8:30 ET (NFP, CPI) is the single most volatile timestamp of
  the week; gold can move 1,000+ pips on those releases and spreads blow
  out 20x. FOMC (14:00 ET) is the same story.
- Funded accounts commonly HARD-PROHIBIT trading +/-10 minutes around
  high-impact (red) news -- violating it forfeits the account even if the
  trade wins.

Events come from ForexFactory's free weekly calendar JSON (a widely used
community feed), cached and refreshed hourly. Fail-safe: if the feed is
unreachable, the guard reports "not blocked" and the desk trades normally
-- the stop-loss/circuit-breaker layers still apply.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

import requests

from bots.paths import data_path

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IMPACT_RANK = {"Low": 0, "Medium": 1, "High": 2}
TIMEOUT = 20


class NewsGuard:
    def __init__(
        self,
        currencies: Tuple[str, ...] = ("USD",),
        min_impact: str = "High",
        minutes_before: int = 10,
        minutes_after: int = 10,
        cache_ttl_minutes: int = 60,
        fetch_fn: Optional[Callable[[], List[dict]]] = None,
    ):
        self.currencies = {c.upper() for c in currencies}
        self.min_rank = IMPACT_RANK.get(min_impact, 2)
        self.before = timedelta(minutes=minutes_before)
        self.after = timedelta(minutes=minutes_after)
        self.cache_ttl = cache_ttl_minutes * 60
        self.fetch_fn = fetch_fn
        self._events: Optional[List[dict]] = None
        self._fetched_at = 0.0
        # Whether the events currently loaded came from a *verified live*
        # source (a successful feed fetch this session) rather than a stale
        # cache fallback or nothing at all. A cached copy still lets us dodge
        # events we already knew about, but it is NOT proof the calendar is
        # current -- a funded account can choose to sit out when the guard
        # can't confirm the schedule is fresh (see is_data_fresh).
        self._data_fresh = False

    def _fetch(self) -> List[dict]:
        if self.fetch_fn is not None:
            self._data_fresh = True  # injected source is treated as live
            return self.fetch_fn()
        cache_file = data_path("news_calendar.json")
        try:
            resp = requests.get(FEED_URL, timeout=TIMEOUT)
            resp.raise_for_status()
            events = resp.json()
            os.makedirs(os.path.dirname(cache_file) or ".", exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as fh:
                json.dump(events, fh)
            self._data_fresh = True
            return events
        except Exception:
            # network down: fall back to the last cached copy if one exists.
            # The cache still protects against events we already knew about,
            # but it is not a verified-current calendar, so mark data as NOT
            # fresh -- an mtime check is unreliable here (the cache can live in
            # the git-committed state dir, so a checkout/restart resets its
            # timestamp to "now" and a week-old file would look brand new).
            self._data_fresh = False
            try:
                with open(cache_file, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                return []

    def _get_events(self) -> List[dict]:
        if self._events is None or (time.time() - self._fetched_at) > self.cache_ttl:
            self._events = self._fetch()
            self._fetched_at = time.time()
        return self._events

    def is_data_fresh(self) -> bool:
        """True only when the guard is running on a verified-live calendar
        (a feed fetch succeeded within the in-memory cache window).

        False means the feed is unreachable and we are either blind or leaning
        on an unverifiable cache -- the moment a funded desk should sit out
        rather than trade as if it had confirmed 'no news right now'. Loads
        events first so the freshness flag reflects the current window."""
        self._get_events()
        return self._data_fresh

    def blackout(
        self,
        now: Optional[datetime] = None,
        currencies: Optional[set] = None,
    ) -> Tuple[bool, str]:
        """(blocked, reason). Blocked when `now` falls inside the window
        around any qualifying event.

        `currencies`, if given, overrides the guard's default set for this
        one call -- lets the desk ask "is there news for THIS symbol's
        currencies?" (e.g. {EUR, JPY} for EURJPY) so a Bank-of-Japan
        decision blacks out the JPY crosses without freezing unrelated
        pairs like AUDUSD."""
        now = now or datetime.now(timezone.utc)
        check = self.currencies if currencies is None else {c.upper() for c in currencies}
        try:
            for event in self._get_events():
                if event.get("country", "").upper() not in check:
                    continue
                if IMPACT_RANK.get(event.get("impact", "Low"), 0) < self.min_rank:
                    continue
                try:
                    when = datetime.fromisoformat(event["date"])
                except (KeyError, ValueError):
                    continue
                if when - self.before <= now <= when + self.after:
                    return True, (
                        f"news blackout: {event.get('title', 'event')} "
                        f"({event.get('country')}, {event.get('impact')}) at "
                        f"{when.strftime('%H:%M %Z')} -- no new entries "
                        f"{int(self.before.total_seconds() // 60)}min before to "
                        f"{int(self.after.total_seconds() // 60)}min after"
                    )
        except Exception as exc:
            return False, f"news guard unavailable ({exc}), trading normally"
        return False, "no high-impact news in window"
