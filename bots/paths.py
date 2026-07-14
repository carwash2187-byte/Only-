"""Where bot state (journal, models, paper account) lives.

Default is the git-ignored ./bot_data directory. Set BOT_DATA_DIR to point
somewhere else -- e.g. BOT_DATA_DIR=paper_state uses a directory that IS
committed to the repo, so the paper-trading track record and the trained
model survive across machines and cloud sessions.
"""

from __future__ import annotations

import os


def data_dir() -> str:
    return os.environ.get("BOT_DATA_DIR", "bot_data")


def data_path(name: str) -> str:
    return os.path.join(data_dir(), name)
