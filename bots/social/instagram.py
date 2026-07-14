"""Watch an Instagram account's posts and Stories for trade calls.

Uses instaloader (github.com/instaloader/instaloader). Instagram actively
fights scrapers -- expect rate limits and periodic checkpoint/login
challenges, worse for Stories specifically since they're not part of any
public API surface at all. Use a throwaway IG account you don't care about
losing, never a real one. Log into that account manually in a browser first
so it looks like a normal login before scripting against it.

Install: pip install instaloader
Setup: export IG_USERNAME / IG_PASSWORD (the throwaway account). The
session is cached to disk after the first login so subsequent runs don't
re-authenticate every time (fewer challenges).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Post:
    shortcode: str
    caption: str
    kind: str  # "post" or "story"


def _login(username: Optional[str] = None, password: Optional[str] = None):
    try:
        import instaloader
    except ImportError as exc:
        raise ImportError("instaloader is not installed. Run: pip install instaloader") from exc

    username = username or os.environ.get("IG_USERNAME", "")
    password = password or os.environ.get("IG_PASSWORD", "")
    if not username or not password:
        raise ValueError("Set IG_USERNAME and IG_PASSWORD (a throwaway account, not your real one).")

    loader = instaloader.Instaloader(download_pictures=False, download_videos=False,
                                     download_video_thumbnails=False, save_metadata=False,
                                     quiet=True)
    session_file = os.path.join(os.path.expanduser("~"), f".instaloader-{username}-session")
    try:
        loader.load_session_from_file(username, session_file)
    except FileNotFoundError:
        loader.login(username, password)
        loader.save_session_to_file(session_file)
    return loader, instaloader


def recent_posts(target_username: str, limit: int = 5) -> List[Post]:
    loader, instaloader = _login()
    profile = instaloader.Profile.from_username(loader.context, target_username)
    posts = []
    for i, post in enumerate(profile.get_posts()):
        if i >= limit:
            break
        posts.append(Post(shortcode=post.shortcode, caption=post.caption or "", kind="post"))
    return posts


def recent_stories(target_username: str) -> List[Post]:
    """Requires the logged-in throwaway account to already follow the
    target (Stories aren't visible to accounts that don't)."""
    loader, instaloader = _login()
    profile = instaloader.Profile.from_username(loader.context, target_username)
    posts = []
    for story in loader.get_stories(userids=[profile.userid]):
        for item in story.get_items():
            posts.append(
                Post(shortcode=str(item.mediaid), caption=item.caption or "", kind="story")
            )
    return posts
