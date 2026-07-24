"""Where bot state (journal, models, paper account) lives.

Default is the git-ignored ./bot_data directory. Set BOT_DATA_DIR to point
somewhere else -- e.g. BOT_DATA_DIR=paper_state uses a directory that IS
committed to the repo, so the paper-trading track record and the trained
model survive across machines and cloud sessions.
"""

from __future__ import annotations

import json
import os


def data_dir() -> str:
    return os.environ.get("BOT_DATA_DIR", "bot_data")


def data_path(name: str) -> str:
    return os.path.join(data_dir(), name)


def safe_json_load(path: str, default: object = None) -> object:
    """Read a JSON state file, self-healing on corruption instead of
    crashing (session 51: the same fix trade_journal.json got in session
    49, generalized -- risk.py's three drawdown/challenge guards,
    qtable.json, and the paper broker's account file all had the exact
    same unprotected `json.load()`, which means a partial write from a
    killed process, a stray git conflict marker, or a bad rebase would
    crash desk construction on EVERY future cycle, forever -- including
    the guards that exist specifically to stop the account from losing
    too much, which is the one place a silent crash is least acceptable).

    Missing file -> `default` (or {}), no error, same as every existing
    caller already expected. A file that exists but won't parse gets
    backed up with a timestamp (evidence preserved, never silently
    discarded) and `default` is returned so the desk keeps running
    instead of dying on the same broken file every 5-minute cycle."""
    default = {} if default is None else default
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        import shutil
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = f"{path}.corrupt-{stamp}.bak"
        try:
            shutil.copy2(path, backup_path)
        except OSError:
            pass
        print(
            f"[self-heal] {path} failed to parse ({exc}) -- backed up to "
            f"{backup_path} and continuing with a fresh default instead of "
            "crashing every future cycle"
        )
        return default
