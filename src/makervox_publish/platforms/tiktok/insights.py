"""Reading back what the account did: per-post counts and follower history.

WHY ``video.list`` IS WORTH THE SCOPE
-------------------------------------
It returns the account's own posts WITH view/like/comment/share counts, which is
what makes performance analysis possible without hand-exporting from the
creator tools. A posting schedule can then be retuned from real numbers instead
of from guesses.

WHY THE FOLLOWER SERIES IS A CSV
--------------------------------
The failure it fixes was not "we cannot see the number" — a dashboard shows the
number. It was that nobody could see the number's HISTORY. One row a day, in
plain text next to the code, is the cheapest thing that fixes that.

⚠️ ``user.info.basic`` RETURNS IDENTITY ONLY
--------------------------------------------
open_id, display name, avatar. Asking it for ``follower_count`` is an ERROR, not
a null field. Follower stats need ``user.info.stats`` — and because scopes are
frozen at authorization, an account authorized before that scope was added must
RE-AUTHORIZE. A refresh will not add it, and the raw API message does not say so.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any, Dict, List, Optional, Tuple

from makervox_publish.errors import ReauthorizationRequired
from makervox_publish.logging import get_logger
from makervox_publish.platforms.tiktok.errors import is_scope_error, raise_for_api_error
from makervox_publish.state.atomic import ensure_dir
from makervox_publish.state.paths import expand

__all__ = ["TikTokInsights", "VIDEO_FIELDS", "USER_STAT_FIELDS", "FOLLOWER_CSV_HEADER"]

log = get_logger(__name__)

VIDEO_FIELDS = (
    "id,create_time,title,video_description,duration,"
    "view_count,like_count,comment_count,share_count,share_url"
)

USER_STAT_FIELDS = "follower_count,following_count,likes_count,video_count"

FOLLOWER_CSV_HEADER = "date,account,followers,source"


class TikTokInsights:
    """``user/info`` and ``video/list``, plus the append-only follower series."""

    def __init__(self, cfg, auth, http, *, state_cfg=None) -> None:
        self.cfg = cfg
        self.auth = auth
        self.http = http
        self.state_cfg = state_cfg

    # -- account counters ---------------------------------------------------- #
    def stats(self, account: str) -> Dict[str, Any]:
        """Followers, following, likes and post count for ``account``.

        ⚠️ Needs ``user.info.stats``. An account authorized with only
        ``user.info.basic`` comes back scope-denied, and the fix is a RE-AUTH,
        not a refresh — a refresh cannot widen scopes.
        """
        response = self.http.get(
            "{0}/user/info/".format(self.cfg.api_base),
            kind="insights",
            params={"fields": USER_STAT_FIELDS},
            headers=self._headers(account),
        )
        payload = self.http.json_body(response)
        if is_scope_error(payload):
            raise ReauthorizationRequired("tiktok", account, ["user.info.stats"])
        data = raise_for_api_error(payload, "user/info failed", account=account)
        user = data.get("user")
        return user if isinstance(user, dict) else {}

    def record_followers(self, account: str, when: Optional[str] = None) -> int:
        """Append today's follower count. Idempotent per (date, account).

        NEVER writes two rows for one day. The job may run more than once, and a
        duplicate day silently turns a flat day into a fake datapoint the moment
        anyone plots the series.
        """
        metrics = self.cfg.metrics
        day = when or dt.datetime.now(dt.timezone.utc).date().isoformat()
        user = self.stats(account)
        count = user.get("follower_count")
        if count is None:
            raise ReauthorizationRequired("tiktok", account, ["user.info.stats"])

        if not metrics.follower_log_enabled:
            log.info("tiktok %s %s: %s followers (logging disabled; set "
                     "platforms.tiktok.metrics.follower_log.enabled = true to "
                     "keep a series)", account, day, count)
            return int(count)

        path = expand(metrics.follower_log_path)
        if metrics.follower_log_one_row_per_day and self._already_recorded(path, day, account):
            log.info("tiktok %s: %s already recorded — leaving it", account, day)
            return int(count)

        ensure_dir(os.path.dirname(path) or ".",
                   int(getattr(self.state_cfg, "dir_mode", 0o700)))
        fresh = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as handle:
            if fresh:
                handle.write(FOLLOWER_CSV_HEADER + "\n")
            handle.write("{0},{1},{2},api\n".format(day, account, count))
        log.info("tiktok %s %s: %s followers (%s posts, %s likes)",
                 account, day, count, user.get("video_count"), user.get("likes_count"))
        return int(count)

    @staticmethod
    def _already_recorded(path: str, day: str, account: str) -> bool:
        prefix = "{0},{1},".format(day, account)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return any(line.startswith(prefix) for line in handle)
        except FileNotFoundError:
            return False
        except OSError as exc:
            # Fail OPEN on an unreadable log: skipping the row would lose a real
            # datapoint, and a duplicate is visible and fixable. Say so.
            log.warning("cannot read the follower log %s (%s); recording the row "
                        "anyway, check for a duplicate line", path, exc)
            return False

    # -- per-post performance ------------------------------------------------ #
    def list_videos(
        self,
        account: str,
        max_count: Optional[int] = None,
        cursor: Optional[Any] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[Any], bool]:
        """One page of the account's own posts. Returns ``(videos, cursor, has_more)``.

        The API caps ``max_count`` at 20 regardless of what is asked for, so the
        config default is 20 and anything larger is clamped rather than silently
        producing short pages.
        """
        page = int(max_count or self.cfg.metrics.list_page_size)
        body = {"max_count": min(page, 20)}
        if cursor:
            body["cursor"] = cursor
        response = self.http.post(
            "{0}/video/list/?fields={1}".format(self.cfg.api_base, VIDEO_FIELDS),
            kind="insights",
            headers=self._headers(account, json_body=True),
            json=body,
        )
        payload = self.http.json_body(response)
        if is_scope_error(payload):
            # Surface the scope problem explicitly: it is the expected first
            # failure here, and the fix (re-auth, not refresh) is not obvious
            # from the raw message.
            raise ReauthorizationRequired("tiktok", account, ["video.list"])
        data = raise_for_api_error(payload, "video/list failed", account=account)
        videos = data.get("videos") or []
        return list(videos), data.get("cursor"), bool(data.get("has_more"))

    def all_videos(self, account: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Every post, paged.

        Stops at ``limit`` (``metrics.max_videos``) so a large account cannot
        loop forever, and stops on a repeated cursor so a server that stops
        advancing cannot either — an unbounded pager against a paid API is a
        billing incident, not a slow function.
        """
        budget = int(limit or self.cfg.metrics.max_videos)
        page_size = int(self.cfg.metrics.list_page_size)
        out = []  # type: List[Dict[str, Any]]
        cursor = None
        seen_cursors = set()
        while len(out) < budget:
            videos, cursor, more = self.list_videos(account, page_size, cursor)
            out.extend(videos)
            if not more or not videos or not cursor:
                break
            if cursor in seen_cursors:
                log.warning("tiktok video/list returned a repeated cursor for %r; "
                            "stopping after %d posts", account, len(out))
                break
            seen_cursors.add(cursor)
        return out[:budget]

    # -- internals ----------------------------------------------------------- #
    def _headers(self, account: str, json_body: bool = False) -> Dict[str, str]:
        headers = {"Authorization": "Bearer {0}".format(self.auth.access_token(account))}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers
