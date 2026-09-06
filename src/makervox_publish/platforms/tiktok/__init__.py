"""makervox_publish.platforms.tiktok — the Content Posting API, self-hosted.

You bring your own TikTok developer app. There is no shared app id, no hosted
OAuth broker, and no default redirect URI pointing anywhere: ``redirect_uri``
has NO default and raises if it is needed unset, because a default there points
at somebody else's domain and still produces a working-looking OAuth screen.

Typical use::

    from makervox_publish import Config
    from makervox_publish.platforms.tiktok import TikTokClient

    cfg = Config.load()
    tiktok = TikTokClient.from_config(cfg)

    ok, detail = tiktok.auto_post("moonlit", "/path/to/reel.mp4")

VENDOR REALITIES THIS PACKAGE DOCUMENTS RATHER THAN HIDES
---------------------------------------------------------
* Scopes are FIXED at authorization; a refresh never widens them, so a scope
  error means RE-AUTH and not retry.
* The refresh token ROTATES ON USE — a successful refresh spends the old one,
  so any deployment running more than one process MUST keep the lock.
* Direct posting needs an app AUDIT; an unaudited app is clamped to SELF_ONLY.
* ``creator_info`` must precede a direct post and must never be cached.
* Branded content cannot be posted privately (refused locally, before an upload
  is spent on it).
* The pending-share inbox cap (about five) makes high-frequency automated
  drafting self-defeating.

Abstracting any of these away would produce a package that lies about what it
can do.
"""

from __future__ import annotations

from makervox_publish.platforms.tiktok.auth import SETUP_HINT, TikTokAuth, credential_spec
from makervox_publish.platforms.tiktok.client import PLATFORM, TikTokClient
from makervox_publish.platforms.tiktok.errors import TikTokApiError
from makervox_publish.platforms.tiktok.insights import (
    USER_STAT_FIELDS,
    VIDEO_FIELDS,
    TikTokInsights,
)
from makervox_publish.platforms.tiktok.upload import ChunkPlan, plan_chunks, upload_chunks

__all__ = [
    "PLATFORM",
    "TikTokClient",
    "TikTokAuth",
    "TikTokInsights",
    "TikTokApiError",
    "ChunkPlan",
    "plan_chunks",
    "upload_chunks",
    "credential_spec",
    "SETUP_HINT",
    "VIDEO_FIELDS",
    "USER_STAT_FIELDS",
]
