"""The TikTok Content Posting API client.

TWO TIERS, AND THE DIFFERENCE MATTERS
-------------------------------------
``inbox``
    Upload lands in the account's own inbox/drafts and WAITS FOR A HUMAN TAP.
    Works as soon as your app exists; no audit. Scope: ``video.upload``.
``direct``
    Posts to the profile with the caption already set. Requires TikTok to AUDIT
    your app first. **An unaudited app is clamped to SELF_ONLY** no matter what
    privacy level is passed, so an unaudited "direct post" is a private post.
    Scope: ``video.publish``.

⚠️ AN AUTOMATED DRAFT QUEUE IS SELF-LIMITING
--------------------------------------------
Inbox uploads consume a PENDING-SHARE quota that only a human tap clears, and
the cap is roughly five. Measured: moving from four uploads a day to five put
the day's later uploads straight into ``spam_risk_too_many_pending_share``, and
they simply failed. If nobody is going to tap them, the uploads are not free —
they burn the quota that the ones you DO want are competing for. That is why
``auto_publish`` is per-account and why a deployment-wide disable list exists;
the package ships that list EMPTY, because the original shipped two real account
names in it and any adopter whose account happened to share a name silently got
no upload at all.

⚠️ ``creator_info`` IS NEVER CACHED
-----------------------------------
It must precede a direct post: the platform's content-sharing guidelines require
the review screen to show the creator's own nickname, offer only the privacy
levels this account actually allows, grey out interaction options the account
has turned off, and respect ``max_video_post_duration_sec``. Caching it is
unsafe in a specific way — a creator can flip their account to private at any
moment, and a stale ``PUBLIC_TO_EVERYONE`` option would post against their
current setting.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

from makervox_publish.credentials import Credentials
from makervox_publish.errors import PublishError
from makervox_publish.http import HttpClient, TransportError
from makervox_publish.logging import get_logger
from makervox_publish.media.ffmpeg import FfmpegTools
from makervox_publish.media.transcode import TranscodeSettings, sized_for_upload
from makervox_publish.platforms import PublishResult
from makervox_publish.platforms.tiktok.auth import SETUP_HINT, TikTokAuth, credential_spec
from makervox_publish.platforms.tiktok.errors import TikTokApiError, raise_for_api_error
from makervox_publish.platforms.tiktok.insights import TikTokInsights
from makervox_publish.platforms.tiktok.upload import plan_chunks, upload_chunks
from makervox_publish.state.token_store import build_token_store

__all__ = ["TikTokClient", "PLATFORM"]

log = get_logger(__name__)

PLATFORM = "tiktok"

INBOX_INIT = "/post/publish/inbox/video/init/"
DIRECT_INIT = "/post/publish/video/init/"

#: Statuses that mean the upload is done with, one way or the other.
_TERMINAL_OK = ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE")
_TERMINAL_BAD = ("FAILED", "EXPIRED")


class TikTokClient:
    """Publish to, and read back from, one TikTok developer app.

    Construct with :meth:`from_config`. One client serves every account in the
    config: an account is a key in the token store, not a separate client.
    """

    def __init__(
        self,
        cfg,
        *,
        auth: TikTokAuth,
        http: HttpClient,
        insights: Optional[TikTokInsights] = None,
        ffmpeg: Optional[FfmpegTools] = None,
        transcode: Optional[TranscodeSettings] = None,
        program_name: str = "makervox_publish",
        auto_publish_for=None,
    ) -> None:
        self.cfg = cfg
        self.auth = auth
        self.http = http
        self.insights = insights or TikTokInsights(cfg, auth, http)
        self.ffmpeg = ffmpeg
        self.transcode = transcode or TranscodeSettings()
        self.program_name = program_name
        #: callable(account_name) -> bool, supplied by Config so per-account and
        #: deployment-wide switches are resolved in one place.
        self._auto_publish_for = auto_publish_for
        self._creator_info_cache = {}  # type: Dict[str, Tuple[float, Dict[str, Any]]]

    # -- construction -------------------------------------------------------- #
    @classmethod
    def from_config(
        cls,
        config,
        *,
        credentials: Optional[Credentials] = None,
        http: Optional[HttpClient] = None,
        token_store: Any = None,
    ) -> "TikTokClient":
        """Build from a whole :class:`makervox_publish.Config`."""
        cfg = config.platforms.tiktok
        creds = credentials or Credentials.from_config(config.credentials)
        client = http or HttpClient.from_config(config.http)
        store = token_store or build_token_store(
            config.token_store_spec(cfg.tokens.store),
            config.state,
            cache_ttl_s=cfg.tokens.cache_ttl_s,
        )
        auth = TikTokAuth(
            cfg,
            credentials=creds,
            token_store=store,
            http=client,
            program_name=config.cli.program_name,
            account_scopes={
                name: account.tiktok_scopes
                for name, account in config.accounts.items()
                if account.tiktok_scopes
            },
        )
        tools = FfmpegTools.from_config(config.media)
        return cls(
            cfg,
            auth=auth,
            http=client,
            insights=TikTokInsights(cfg, auth, client, state_cfg=config.state),
            ffmpeg=tools,
            transcode=TranscodeSettings.from_config(config.media.transcode),
            program_name=config.cli.program_name,
            auto_publish_for=lambda name: cfg.auto_publish_for(config.account(name)),
        )

    # -- readiness ----------------------------------------------------------- #
    @property
    def credential_spec(self):
        return credential_spec(self.cfg)

    def is_configured(self) -> bool:
        return self.auth.is_configured()

    def not_configured_reason(self) -> str:
        return self.auth.not_configured_reason()

    def auto_publish_enabled(self, account: str) -> bool:
        if account in self.cfg.publish.auto_publish_disabled_accounts:
            return False
        if self._auto_publish_for is not None:
            return bool(self._auto_publish_for(account))
        return self.cfg.publish.mode == "direct"

    # -- creator capabilities ------------------------------------------------ #
    def creator_info(self, account: str) -> Dict[str, Any]:
        """The authorizing creator's posting capabilities. REQUIRED before a direct post.

        Returns ``creator_nickname``, ``creator_username``, ``creator_avatar_url``,
        ``privacy_level_options``, ``comment_disabled``, ``duet_disabled``,
        ``stitch_disabled``, ``max_video_post_duration_sec``.

        ⚠️ ``creator_info_cache_s`` defaults to 0 — never cached. Do not raise
        it; see the module docstring.
        """
        ttl = float(self.cfg.publish.creator_info_cache_s)
        if ttl > 0:
            hit = self._creator_info_cache.get(account)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        response = self.http.post(
            "{0}/post/publish/creator_info/query/".format(self.cfg.api_base),
            kind="small_write",
            headers=self._headers(account),
        )
        payload = self.http.json_body(response)
        data = raise_for_api_error(payload, "creator_info failed", account=account)
        if ttl > 0:
            self._creator_info_cache[account] = (time.time(), data)
        return data

    def privacy_level_options(self, account: str) -> List[str]:
        options = self.creator_info(account).get("privacy_level_options")
        return list(options or [])

    # -- publishing ---------------------------------------------------------- #
    def post_to_inbox(self, account: str, video_path: str) -> str:
        """Upload to the account's inbox/drafts. Returns ``publish_id``.

        Nothing about this is automatic afterwards: the reel sits in the app
        until a human opens it and taps Post.
        """
        return self._publish(account, video_path, INBOX_INIT, None)

    def direct_post(
        self,
        account: str,
        video_path: str,
        caption: str = "",
        *,
        privacy_level: Optional[str] = None,
        disable_comment: bool = False,
        disable_duet: bool = False,
        disable_stitch: bool = False,
        brand_organic_toggle: bool = False,
        brand_content_toggle: bool = False,
    ) -> str:
        """Publish straight to the profile. Requires an AUDITED app.

        ``privacy_level`` MUST come from this creator's own
        ``privacy_level_options`` and MUST be chosen by a human — the API
        forbids a pre-selected default, so there is deliberately no fallback
        here. An unaudited app is clamped to SELF_ONLY regardless of what is
        passed.
        """
        options = self.privacy_level_options(account)
        if not privacy_level:
            raise PublishError(
                "direct_post needs an explicit privacy_level chosen by a person; "
                "the API forbids a pre-selected default. This creator currently "
                "offers: {0}".format(", ".join(options) or "(none reported)")
            )
        if options and privacy_level not in options:
            raise PublishError(
                "privacy_level {0!r} is not one this creator offers ({1}). The "
                "list changes when they change their account settings, which is "
                "why it is fetched fresh every time.".format(
                    privacy_level, ", ".join(options)
                )
            )

        info = {
            "title": (caption or "")[: self.cfg.upload.caption_max_chars],
            "privacy_level": privacy_level,
            "disable_comment": bool(disable_comment),
            "disable_duet": bool(disable_duet),
            "disable_stitch": bool(disable_stitch),
        }
        if brand_organic_toggle or brand_content_toggle:
            # Commercial-content disclosure. Branded (third-party) content may
            # NOT be posted privately, so that combination is refused HERE
            # rather than after a full upload has been spent on it.
            if (brand_content_toggle and privacy_level == "SELF_ONLY"
                    and self.cfg.publish.refuse_branded_private):
                raise PublishError(
                    "branded content cannot be posted with SELF_ONLY visibility "
                    "— the platform rejects that combination after the upload "
                    "completes, so it is refused before one is spent"
                )
            info["brand_organic_toggle"] = bool(brand_organic_toggle)
            info["brand_content_toggle"] = bool(brand_content_toggle)
        return self._publish(account, video_path, DIRECT_INIT, info)

    def _publish(self, account: str, video_path: str, endpoint: str,
                 post_info: Optional[Dict[str, Any]],
                 progress: Optional[Dict[str, int]] = None) -> str:
        full = os.path.abspath(os.path.expanduser(video_path))
        if not os.path.isfile(full):
            raise FileNotFoundError(full)

        with sized_for_upload(
            full, self.cfg.upload.max_video_bytes,
            tools=self.ffmpeg or FfmpegTools.discover(),
            settings=self.transcode,
        ) as media:
            plan = plan_chunks(media.final_bytes, self.cfg.upload.max_chunk_bytes)
            body = {"source_info": plan.as_source_info()}  # type: Dict[str, Any]
            if post_info is not None:
                body["post_info"] = post_info

            response = self.http.post(
                "{0}{1}".format(self.cfg.api_base, endpoint),
                kind="media_create",
                headers=self._headers(account, json_body=True),
                json=body,
            )
            payload = self.http.json_body(response)
            data = raise_for_api_error(
                payload, "publish init failed", account=account,
                hint="a spam_risk code usually means the inbox already holds "
                     "its cap of pending shares (about five); those clear only "
                     "when a person taps Post",
            )
            upload_url = data.get("upload_url")
            publish_id = data.get("publish_id")
            if not upload_url or not publish_id:
                raise TikTokApiError("publish init returned no upload target",
                                     payload=payload, account=account)

            if progress is not None:
                # From here on a retry of the OUTER call would produce a SECOND
                # pending share against a cap of roughly five. See auto_post.
                progress["init_done"] = 1
            upload_chunks(
                self.http, upload_url, media.path, plan,
                accepted_statuses=self.cfg.upload.accepted_statuses,
            )
            if progress is not None:
                progress["uploaded"] = 1
            log.info("tiktok: uploaded %s for %r as %s (%d chunk(s), %d bytes%s)",
                     os.path.basename(media.source_path), account, publish_id,
                     plan.total_chunk_count, media.final_bytes,
                     ", transcoded" if media.transcoded else "")
            return publish_id

    # -- status -------------------------------------------------------------- #
    def status(self, account: str, publish_id: str) -> Dict[str, Any]:
        """Raw status envelope for a ``publish_id``."""
        response = self.http.post(
            "{0}/post/publish/status/fetch/".format(self.cfg.api_base),
            kind="poll",
            headers=self._headers(account, json_body=True),
            json={"publish_id": publish_id},
        )
        return self.http.json_body(response)

    def confirm(self, account: str, publish_id: str,
                attempts: Optional[int] = None,
                delay_s: Optional[float] = None) -> Tuple[bool, str]:
        """Poll until the platform says the upload really landed.

        Turns an optimistic "sent to drafts" into a VERIFIED result, so a
        notification never reports a false positive.

        Still ``PROCESSING`` after the budget counts as ACCEPTED
        (``processing_is_accepted``): the bytes are in the platform's hands by
        then, and reporting a failure would send someone chasing a post that is
        about to appear.
        """
        confirm_cfg = self.cfg.publish.confirm
        tries = int(attempts if attempts is not None else confirm_cfg.attempts)
        pause = float(delay_s if delay_s is not None else confirm_cfg.delay_s)
        last = "UNKNOWN"
        for _ in range(max(tries, 1)):
            try:
                payload = self.status(account, publish_id) or {}
                data = payload.get("data") or {}
                state = data.get("status") or payload.get("status") or "UNKNOWN"
            except Exception as exc:  # noqa: BLE001 - polling must not raise
                state = "ERR {0}".format(str(exc)[:80])
            last = str(state)
            if last in _TERMINAL_OK:
                return True, last
            if last in _TERMINAL_BAD:
                return False, last
            time.sleep(pause)
        if confirm_cfg.processing_is_accepted:
            return (last.startswith("PROCESSING") or last == "UNKNOWN"), last
        return False, last

    # -- the safe, pipeline-facing entry point ------------------------------- #
    def auto_post(self, account: str, video_path: str,
                  tries: Optional[int] = None) -> PublishResult:
        """Upload for ``account`` if it is connected and enabled. NEVER raises.

        Retries transient network drops, but only while nothing has been
        uploaded yet. Once the init call has succeeded, a retry of the whole
        function mints a SECOND pending share against a cap of roughly five —
        so a failure after that point is reported, not retried.

        API rejections are never retried at all: re-sending an upload the
        platform refused for spam or rate reasons makes the rejection worse.
        """
        budget = max(int(tries if tries is not None else self.http.policy.attempts), 1)
        try:
            if not self.is_configured():
                return self._refused(account, self.not_configured_reason())
            if not self.auto_publish_enabled(account):
                return self._refused(
                    account,
                    "auto-publish is disabled for this account "
                    "(accounts.{0}.tiktok.auto_publish, or "
                    "platforms.tiktok.publish.auto_publish_disabled_accounts). "
                    "Inbox uploads nobody taps still consume the pending-share "
                    "cap.".format(account),
                )
            if not self.auth.is_authorized(account):
                return self._refused(
                    account,
                    "not authorized — run: {0}".format(self.auth.reauth_hint(account)),
                )

            for attempt in range(1, budget + 1):
                progress = {}  # type: Dict[str, int]
                try:
                    publish_id = self._publish(account, video_path, INBOX_INIT, None,
                                               progress=progress)
                    return PublishResult(True, platform=PLATFORM, account=account,
                                         media_id=publish_id, post_id=publish_id)
                except Exception as exc:  # noqa: BLE001
                    retryable = (
                        attempt < budget
                        and not progress.get("init_done")
                        and _is_transport(exc)
                    )
                    if not retryable:
                        raise
                    delay = self.http.policy.backoff_s * attempt
                    log.warning(
                        "tiktok: network error before upload started for %r "
                        "(%s); retry %d/%d in %.0fs",
                        account, type(exc).__name__, attempt, budget - 1, delay,
                    )
                    time.sleep(delay)
        except Exception as exc:  # noqa: BLE001 - must never break a pipeline
            log.warning("tiktok auto_post failed for %r: %s: %s",
                        account, type(exc).__name__, exc)
            return self._refused(account, "{0}: {1}".format(type(exc).__name__, exc))
        # Only reachable if `budget` iterations all continued, which the
        # retryable guard prevents. Explicit so no edit can return None here.
        return self._refused(account, "upload was never attempted")

    @staticmethod
    def _refused(account: str, reason: str) -> PublishResult:
        """An expected, non-fatal "no". Never an exception: one dark account
        must not abort a batch containing three working ones."""
        return PublishResult(False, platform=PLATFORM, account=account, reason=reason)

    # -- delegated reads ----------------------------------------------------- #
    def stats(self, account: str) -> Dict[str, Any]:
        return self.insights.stats(account)

    def record_followers(self, account: str, when: Optional[str] = None) -> int:
        return self.insights.record_followers(account, when)

    def list_videos(self, account: str, max_count: Optional[int] = None,
                    cursor: Optional[Any] = None):
        return self.insights.list_videos(account, max_count, cursor)

    def all_videos(self, account: str, limit: Optional[int] = None):
        return self.insights.all_videos(account, limit)

    # -- OAuth passthrough --------------------------------------------------- #
    def authorize_url(self, account: str, state: Optional[str] = None) -> str:
        return self.auth.authorize_url(account, state)

    def exchange_code(self, account: str, code: str) -> Dict[str, Any]:
        return self.auth.exchange_code(account, code)

    # -- internals ----------------------------------------------------------- #
    def _headers(self, account: str, json_body: bool = False) -> Dict[str, str]:
        headers = {"Authorization": "Bearer {0}".format(self.auth.access_token(account))}
        if json_body:
            headers["Content-Type"] = "application/json; charset=UTF-8"
        return headers

    def describe(self) -> str:
        return "{0} | setup: {1}".format(
            self.auth.describe(), SETUP_HINT.format(prog=self.program_name)
        )


def publisher(config, **kwargs) -> TikTokClient:
    """Factory discovered by :func:`makervox_publish.platforms.publisher_for`."""
    return TikTokClient.from_config(config, **kwargs)


def _is_transport(exc: BaseException) -> bool:
    """Whether a failure never reached the API (so a retry is not a duplicate)."""
    return isinstance(exc, TransportError)
