"""Watch a YouTube channel for new videos and read their transcripts.

No API key needed: channel video lists come from YouTube's public per-channel
RSS feed, and captions come from youtube-transcript-api (reads YouTube's own
caption track, works for any video with captions -- auto-generated included).

Install: pip install youtube-transcript-api
Find a channel ID: view-source any of the channel's pages, search for
"channelId", or use https://www.youtube.com/@handle -> right-click -> View
Page Source -> search "externalId".
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional

import requests

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
TIMEOUT = 30


@dataclass
class Video:
    video_id: str
    title: str
    published: str


def recent_videos(channel_id: str, limit: int = 5) -> List[Video]:
    resp = requests.get(RSS_URL.format(channel_id=channel_id), timeout=TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    videos = []
    for entry in root.findall("atom:entry", ns)[:limit]:
        videos.append(
            Video(
                video_id=entry.findtext("yt:videoId", default="", namespaces=ns),
                title=entry.findtext("atom:title", default="", namespaces=ns),
                published=entry.findtext("atom:published", default="", namespaces=ns),
            )
        )
    return videos


def transcript_text(video_id: str) -> Optional[str]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:
        raise ImportError(
            "youtube-transcript-api is not installed. Run: pip install youtube-transcript-api"
        ) from exc
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id)
        return " ".join(snippet.text for snippet in fetched)
    except Exception as exc:
        print(f"[youtube] no transcript for {video_id}: {exc}")
        return None


def resolve_channel_id(handle_or_url: str) -> str:
    """Best-effort: accepts a raw channel ID (UC...) unchanged, otherwise
    scrapes the channel page for its externalId."""
    if re.fullmatch(r"UC[\w-]{22}", handle_or_url):
        return handle_or_url
    url = handle_or_url
    if not url.startswith("http"):
        url = f"https://www.youtube.com/{url.lstrip('@')}" if not url.startswith("@") else \
              f"https://www.youtube.com/{url}"
        if "@" not in url:
            url = f"https://www.youtube.com/@{handle_or_url.lstrip('@')}"
    resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    match = re.search(r'"channelId":"(UC[\w-]{22})"', resp.text)
    if not match:
        raise ValueError(f"Could not resolve a channel ID from {handle_or_url}")
    return match.group(1)
