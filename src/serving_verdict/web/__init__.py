"""Self-contained offline web UI assets.

The UI is plain HTML/CSS/JS served from the package — no React/Node toolchain,
no CDNs, no network access at runtime (MVP spec). ``web_root()`` returns the
directory holding the assets (source tree in dev, wheel-installed dir in prod).
"""
from __future__ import annotations

from pathlib import Path


def web_root() -> Path:
    return Path(__file__).resolve().parent
