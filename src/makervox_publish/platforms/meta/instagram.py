"""Instagram publisher — Reels and photos to the IG account linked to a Page.

PRIMARY PATH (Facebook Page)
----------------------------
A Professional Instagram account linked to a Facebook Page is published through
``graph.facebook.com/{ig-user-id}`` using the **Page token** — there is no
separate Instagram-login token to mint. It needs ``instagram_content_publish``
(plus ``instagram_basic``) on the Page token, and the IG user id is resolved from
the Page's ``instagram_business_account`` field.

This is the path that works. The Instagram-login OAuth kept failing with
"Insufficient Developer Role" on an app that had every scope it asked for, and
going through the Page sidesteps that flow entirely.

LEGACY PATH (Instagram login, graph.instagram.com)
--------------------------------------------------
Still supported for an account that has an IG-login token stored (via
:meth:`InstagramPublisher.connect`), and used only when the Page path cannot
resolve a linked IG account. Note the host is UNVERSIONED — ``graph.instagram.com``
takes no ``/vNN`` prefix, unlike the Graph host next to it. That asymmetry is
real, not a missing constant.

THE FLOW (3 calls, identical on both paths)
-------------------------------------------
create a media container -> poll until FINISHED -> publish.

Instagram **PULLS** the media by URL. You cannot upload bytes to it. So a local
file is staged somewhere publicly fetchable, published, and deleted again — see
:mod:`makervox_publish.platforms.meta.staging`. A staged object that fails to delete stays
publicly readable, so that failure is logged at ERROR with its location rather
than swallowed.

PRODUCT LIMIT, NOT A BUG
------------------------
The publishing API **cannot attach a trending sound** — that is in-app only, and
on this platform a trending sound is the single biggest reach lever there is.
Treat automated posting here as a SAFETY NET so the account is never dark, and
hand-post the pieces where reach actually matters.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional, Tuple

from makervox_publish.credentials import CredentialSpec, Credentials
from makervox_publish.errors import ConfigError, IdentityClash, PublishError
from makervox_publish.http.client import HttpClient, TransportError
from makervox_publish.logging import get_logger
from makervox_publish.media.cover import cover_offset_ms
from makervox_publish.media.ffmpeg import FfmpegTools
from makervox_publish.platforms import PublishResult
from makervox_publish.platforms.meta.staging import content_type_for, staged
from makervox_publish.platforms.meta.tokens import MetaTokenStore, PageCredentials, StoredPageResolver

__all__ = [
    "InstagramPublisher",
    "credential_spec",
    "publisher",
    "IG_USER_ID_CREDENTIAL",
    "IG_TOKEN_CREDENTIAL",
]

log = get_logger(__name__)

#: DEFAULT credential names for the direct/legacy path. They are defaults, not
#: constants: a self-hoster running two Instagram accounts from one environment
#: overrides them rather than being forced into one global pair.
IG_USER_ID_CREDENTIAL = "IG_USER_ID"
IG_TOKEN_CREDENTIAL = "IG_TOKEN"

_SETUP = (
    "Link the Instagram Professional account to a Facebook Page on YOUR OWN "
    "Meta app, grant instagram_content_publish + instagram_basic on the Page "
    "token, and set accounts.<name>.facebook.page_id. Only if you cannot use a "
    "Page: store an Instagram-login token with `makervox_publish auth instagram <account>`."
)


def credential_spec(config: Optional[Any] = None) -> CredentialSpec:
    """What Instagram needs from the credential provider.

    The required list is EMPTY on purpose. On the primary path the credential is
    a Page token living in the shared Meta token store, not an environment
    variable, so declaring ``IG_TOKEN`` as required would report a correctly
    configured account as broken. The optional keys are the direct/legacy path
    and the app secret used for one short-lived -> long-lived exchange.
    """
    app_secret = "META_APP_SECRET"
    if config is not None:
        app_secret = getattr(
            config.platforms.facebook, "app_secret_credential", app_secret
        )
    return CredentialSpec(
        "instagram",
        (),
        _SETUP,
        frozenset({"image", "video"}),
        (IG_USER_ID_CREDENTIAL, IG_TOKEN_CREDENTIAL, app_secret),
    )


class InstagramPublisher:
    """Publishes to one Meta app's Instagram accounts."""

    platform = "instagram"

    def __init__(
        self,
        config: Any,
        *,
        credentials: Optional[Credentials] = None,
        http: Optional[HttpClient] = None,
        tools: Optional[FfmpegTools] = None,
        tokens: Optional[MetaTokenStore] = None,
        page_resolver: Optional[Any] = None,
        stager: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.cfg = config.platforms.instagram
        self.fb_cfg = config.platforms.facebook
        self.credentials = credentials or Credentials.from_config(config.credentials)
        self.http = http or HttpClient.from_config(config.http)
        self.tools = tools if tools is not None else FfmpegTools.from_config(config.media)
        self.spec = credential_spec(config)
        self._tokens = tokens
        self._page_resolver = page_resolver
        self._stager = stager
        self._error_chars = int(
            getattr(config.logging, "provider_error_body_chars", 400)
        )

    # -- lazily built collaborators ------------------------------------------ #
    # Built on first use, not in __init__, so constructing a publisher never
    # touches the filesystem and `post()` with explicit credentials needs no
    # token store at all.
    @property
    def tokens(self) -> MetaTokenStore:
        if self._tokens is None:
            self._tokens = MetaTokenStore.from_config(self.config)
        return self._tokens

    @property
    def page_resolver(self) -> Any:
        if self._page_resolver is None:
            self._page_resolver = StoredPageResolver.from_config(self.config, self.tokens)
        return self._page_resolver

    @property
    def stager(self) -> Any:
        if self._stager is None:
            if self.cfg.staging is None:
                raise ConfigError(
                    "Instagram can only PULL media by URL, so publishing a "
                    "local file needs a staging target. Set "
                    "platforms.instagram.staging.impl.",
                    key="platforms.instagram.staging",
                )
            self._stager = self.cfg.staging.build(group="makervox_publish.media_stagers")
        return self._stager

    # -- hosts ---------------------------------------------------------------- #
    @property
    def graph_base(self) -> str:
        """Versioned Graph host used by the Facebook-Page path."""
        version = self.cfg.api_version or self.fb_cfg.api_version
        return "{0}/{1}".format(self.cfg.graph_base.rstrip("/"), version)

    @property
    def instagram_base(self) -> str:
        """Instagram-login host. UNVERSIONED — it takes no /vNN prefix."""
        return self.cfg.instagram_graph_base.rstrip("/")

    # -- credential resolution ------------------------------------------------ #
    def _linked_ig_user(self, page: PageCredentials) -> Optional[str]:
        """The IG Business account id linked to ``page``, or None if there is none.

        Raises on an API failure. The original swallowed every exception here and
        fell through to the Instagram-login path, which turned "your Page token
        lost a scope" into "this account has no Instagram", and sent whoever was
        debugging it down an OAuth flow that cannot work.
        """
        try:
            response = self.http.get(
                "{0}/{1}".format(self.graph_base, page.page_id),
                params={
                    "fields": "instagram_business_account",
                    "access_token": page.token,
                },
                kind="default",
            )
        except TransportError as exc:
            raise PublishError(
                "could not reach the Graph API to resolve the Instagram account "
                "linked to Page {0}: {1}".format(page.page_id, exc)
            ) from exc

        body = self.http.json_body(response)
        if response.status_code != 200 or "error" in body:
            raise PublishError(
                "resolving the Instagram account linked to Page {0} failed "
                "({1}): {2}".format(
                    page.page_id, response.status_code, self._body_text(response)
                )
            )
        return (body.get("instagram_business_account") or {}).get("id") or None

    def page_credentials(self, account: str) -> Tuple[Optional[str], Optional[str]]:
        """``(ig_user_id, page_token)`` for the Facebook-Page path.

        ``(None, None)`` means "this account has no linked Page / no linked IG
        account" — a legitimate answer that falls through to the legacy path.
        Anything else raises: a resolver that blew up must be loud, because a
        silent downgrade is indistinguishable from an account nobody set up.
        """
        page = self.page_resolver.resolve(account)
        if page is None:
            return None, None

        account_cfg = self.config.accounts.get(account)
        pinned = getattr(account_cfg, "instagram_user_id", None) if account_cfg else None
        if pinned:
            # Configured explicitly; skip the lookup entirely.
            return str(pinned), page.token

        ig_user = self._linked_ig_user(page)
        return (str(ig_user), page.token) if ig_user else (None, None)

    def stored_credentials(self, account: str) -> Tuple[Optional[str], Optional[str]]:
        """``(user_id, token)`` from a stored Instagram-login record (legacy path).

        Refreshes a long-lived token that is nearing expiry on the way past.
        """
        record = self.tokens.instagram_for(account)
        if not record or not record.get("user_id") or not record.get("token"):
            return None, None
        issued_at = float(record.get("ts") or 0)
        age = time.time() - issued_at
        if issued_at and age > float(self.cfg.tokens_refresh_after_s):
            self.refresh(account)
            record = self.tokens.instagram_for(account) or record
        return str(record.get("user_id")), str(record.get("token"))

    def direct_credentials(
        self, creds: Optional[Dict[str, str]] = None
    ) -> Tuple[Optional[str], Optional[str], list]:
        """``(user_id, token, missing)`` from an explicit mapping or the provider.

        A mapping wins when given — a library caller holding its own secrets does
        not have to push them through an environment variable first.
        """
        if creds:
            user_id = creds.get(IG_USER_ID_CREDENTIAL) or creds.get("user_id")
            token = creds.get(IG_TOKEN_CREDENTIAL) or creds.get("token")
            missing = [
                name for name, value in (
                    (IG_USER_ID_CREDENTIAL, user_id), (IG_TOKEN_CREDENTIAL, token)
                ) if not value
            ]
            return user_id, token, missing
        values, missing = self.credentials.require(
            [IG_USER_ID_CREDENTIAL, IG_TOKEN_CREDENTIAL]
        )
        return (
            values.get(IG_USER_ID_CREDENTIAL),
            values.get(IG_TOKEN_CREDENTIAL),
            missing,
        )

    # -- token maintenance ---------------------------------------------------- #
    def refresh(self, account: str) -> bool:
        """Extend a long-lived Instagram-login token.

        The refresh needs NO app secret (``ig_refresh_token``); only the initial
        short-lived -> long-lived exchange does. Long-lived tokens last 60 days,
        so the default refresh window is 50.

        The whole read-modify-write runs inside the store's transaction, which
        re-reads under the lock: Facebook and Instagram share one token file, and
        without it a Page token written between this read and this write would be
        silently dropped.
        """
        with self.tokens.transaction() as txn:
            document = self.tokens.normalize(txn.data)
            record = document["instagram"].get(account)
            if not isinstance(record, dict) or not record.get("token"):
                return False

            # Re-read inside the lock: if another process refreshed while we
            # waited, adopt its token instead of spending ours again.
            issued_at = float(record.get("ts") or 0)
            if issued_at and (time.time() - issued_at) <= float(
                self.cfg.tokens_refresh_after_s
            ):
                log.info(
                    "instagram/%s was refreshed by another process while we "
                    "waited for the lock; adopting that token.", account,
                )
                return True

            try:
                response = self.http.get(
                    "{0}/refresh_access_token".format(self.instagram_base),
                    params={
                        "grant_type": "ig_refresh_token",
                        "access_token": record["token"],
                    },
                    kind="auth",
                )
            except TransportError as exc:
                log.warning("instagram/%s token refresh could not be sent: %s",
                            account, exc)
                return False

            body = self.http.json_body(response)
            token = body.get("access_token")
            if not token:
                log.warning(
                    "instagram/%s token refresh was refused (%s): %s. A refused "
                    "refresh usually means the token already expired or a scope "
                    "was revoked — that needs a RE-AUTHORIZATION, not a retry.",
                    account, response.status_code, self._body_text(response),
                )
                return False

            record["token"] = token
            record["ts"] = int(time.time())
            document["instagram"][account] = record
            txn.save(document)
            log.info("instagram/%s token refreshed (expires in ~%ss)",
                     account, body.get("expires_in", "60d"))
            return True

    def connect(
        self,
        account: str,
        ig_token: str,
        app_secret: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Store an Instagram-login token for ``account`` (the legacy path).

        Resolves the IG user id from the token, exchanges a short-lived token for
        a 60-day one when an app secret is available, and files it under
        ``account``.

        ``app_secret`` falls back to the credential provider using the configured
        name (``platforms.facebook.app_secret_credential``), so the secret never
        has to be typed on a command line where it lands in shell history.
        """
        if app_secret is None:
            app_secret = self.credentials.get(self.fb_cfg.app_secret_credential)

        if app_secret:
            exchanged = self.http.json_body(self.http.get(
                "{0}/access_token".format(self.instagram_base),
                params={
                    "grant_type": "ig_exchange_token",
                    "client_secret": app_secret,
                    "access_token": ig_token,
                },
                kind="auth",
            ))
            if exchanged.get("access_token"):
                ig_token = exchanged["access_token"]
            else:
                log.warning(
                    "short-lived -> long-lived exchange did not return a token; "
                    "storing the token as given, which may expire within hours."
                )

        me = self.http.json_body(self.http.get(
            "{0}/me".format(self.instagram_base),
            params={"fields": "user_id,username", "access_token": ig_token},
            kind="auth",
        ))
        user_id = me.get("user_id") or me.get("id")
        if not user_id:
            # A token that authenticates but returns no user id is the signature
            # of a MISSING SCOPE, not of a bad token. Saying so here saves the
            # next hour of regenerating perfectly good tokens.
            raise PublishError(
                "token check failed — no user id came back, which usually means "
                "the content-publish scope is missing rather than that the token "
                "is wrong: {0}".format(me)
            )

        # CROSS-ACCOUNT AUTHORIZATION GUARD. Platforms issue a token for whichever
        # account the BROWSER was signed into, not the one named on the command
        # line. Saving it anyway files one account's credentials under another's
        # name, and every later post lands on the wrong account.
        owner = self.tokens.instagram_account_for_user_id(str(user_id))
        if owner and owner != account:
            raise IdentityClash(account, owner, str(user_id))

        record = {
            "user_id": str(user_id),
            "username": me.get("username", ""),
            "token": ig_token,
            "ts": int(time.time()),
        }
        self.tokens.save_instagram(account, record)
        return {
            "account": account,
            "user_id": str(user_id),
            "username": me.get("username", ""),
        }

    # -- publishing ----------------------------------------------------------- #
    def auto_post(
        self,
        account: str,
        media_path: str,
        caption: str = "",
    ) -> PublishResult:
        """Publish a local file to ``account``'s Instagram.

        Inert rather than explosive when the account is not wired up: returns
        ``ok=False`` with a reason so a batch covering several platforms is not
        aborted by the one that was never configured.
        """
        if not (media_path and os.path.exists(media_path)):
            return self._fail(account, "media not found: {0}".format(media_path))

        try:
            if self.cfg.auth_path == "instagram_login":
                ig_user, token = self.stored_credentials(account)
                base, path_used = self.instagram_base, "instagram_login"
            else:
                ig_user, token = self.page_credentials(account)
                base, path_used = self.graph_base, "facebook_page"
                if not ig_user:
                    # No linked Page, or the Page has no linked IG account. A
                    # resolver FAILURE would have raised instead of landing here.
                    ig_user, token = self.stored_credentials(account)
                    base, path_used = self.instagram_base, "instagram_login"
        except PublishError as exc:
            # Loud about a real failure, but still not fatal to a batch.
            return self._fail(account, str(exc))

        if not ig_user or not token:
            return self._fail(
                account,
                "no Instagram account resolved for {0!r}: the Page token needs "
                "instagram_content_publish (re-authorize the Page), or connect "
                "an Instagram-login token. {1}".format(account, _SETUP),
            )

        thumb_ms = self._cover_offset(media_path)
        with staged(
            self.stager,
            media_path,
            content_type=content_type_for(media_path, "video/mp4"),
        ) as media:
            return self._publish(
                ig_user, token, caption, media.url,
                base=base, thumb_ms=thumb_ms, account=account, path_used=path_used,
            )

    def post(
        self,
        text: str,
        media_path: str,
        creds: Optional[Dict[str, str]] = None,
        *,
        account: str = "",
    ) -> PublishResult:
        """Single-shot publish with explicit (or provider-resolved) credentials.

        Accepts an ALREADY-HOSTED ``http(s)`` URL and skips staging entirely in
        that case — the caller has already solved the pull-by-URL problem and
        re-uploading their file would be pointless.
        """
        if not media_path:
            return self._fail(account, "instagram needs an image or a video")

        ig_user, token, missing = self.direct_credentials(creds)
        if missing:
            return self._fail(account, self.spec.not_configured_reason(missing))

        if str(media_path).startswith(("http://", "https://")):
            return self._publish(
                ig_user, token, text, media_path,
                base=self.instagram_base, thumb_ms=None, account=account,
                path_used="hosted_url",
            )

        if not os.path.exists(media_path):
            return self._fail(
                account, "instagram needs a hosted URL or an existing local file"
            )

        thumb_ms = self._cover_offset(media_path)
        with staged(
            self.stager,
            media_path,
            content_type=content_type_for(media_path, "video/mp4"),
        ) as media:
            return self._publish(
                ig_user, token, text, media.url,
                base=self.instagram_base, thumb_ms=thumb_ms, account=account,
                path_used="direct",
            )

    def _publish(
        self,
        ig_user: str,
        token: str,
        caption: str,
        url: str,
        *,
        base: str,
        thumb_ms: Optional[int],
        account: str = "",
        path_used: str = "",
    ) -> PublishResult:
        """container -> poll -> publish. The same three calls on both paths."""
        is_video = self._is_video(url)
        params = {
            "caption": caption or "",
            "access_token": token,
            ("video_url" if is_video else "image_url"): url,
        }
        if is_video:
            params["media_type"] = "REELS"
            if thumb_ms is not None and self.cfg.publish.send_thumb_offset:
                # Milliseconds into the video to grab the cover from. Without it
                # the platform takes frame 0, and a clip that fades in from black
                # gives you a black tile on the profile grid.
                params["thumb_offset"] = int(thumb_ms)

        try:
            created = self.http.post(
                "{0}/{1}/media".format(base, ig_user), data=params, kind="media_create"
            )
        except TransportError as exc:
            return self._fail(account, "media container request failed: {0}".format(exc))

        body = self.http.json_body(created)
        if created.status_code != 200 or "id" not in body:
            return self._fail(
                account,
                "container {0}: {1}".format(created.status_code, self._body_text(created)),
            )
        container_id = body["id"]

        if is_video:
            outcome = self._await_container(container_id, token, base, account)
            if outcome is not None:
                return outcome

        try:
            published = self.http.post(
                "{0}/{1}/media_publish".format(base, ig_user),
                data={"creation_id": container_id, "access_token": token},
                kind="small_write",
            )
        except TransportError as exc:
            # The container exists and may still be publishable by hand; say so
            # rather than implying nothing happened.
            return self._fail(
                account,
                "publish request failed after the container was created "
                "(container {0}): {1}".format(container_id, exc),
            )

        result = self.http.json_body(published)
        if published.status_code != 200 or "id" not in result:
            return self._fail(
                account,
                "publish {0}: {1}".format(
                    published.status_code, self._body_text(published)
                ),
            )
        log.info("instagram/%s published %s via %s", account or "-",
                 result["id"], path_used or "unknown path")
        return PublishResult(
            True,
            platform=self.platform,
            account=account,
            media_id=str(container_id),
            post_id=str(result["id"]),
            reason="published {0}".format(result["id"]),
            extra={"path": path_used, "thumb_offset_ms": thumb_ms},
        )

    def _await_container(
        self, container_id: str, token: str, base: str, account: str
    ) -> Optional[PublishResult]:
        """Poll until FINISHED. Returns None on success, a failure otherwise.

        A reel MUST finish processing before it can be published; publishing a
        container that is still processing is rejected.
        """
        attempts = int(self.cfg.publish.poll_attempts)
        interval = float(self.cfg.publish.poll_interval_s)
        for _ in range(attempts):
            try:
                response = self.http.get(
                    "{0}/{1}".format(base, container_id),
                    params={"fields": "status_code", "access_token": token},
                    kind="poll",
                )
            except TransportError as exc:
                return self._fail(
                    account, "could not read reel processing status: {0}".format(exc)
                )
            status = self.http.json_body(response).get("status_code")
            if status == "FINISHED":
                return None
            if status == "ERROR":
                return self._fail(
                    account,
                    "reel processing error: {0}".format(self._body_text(response)),
                )
            time.sleep(interval)
        else:
            # The for/else IS the timeout branch: reaching it means the budget
            # ran out without FINISHED or ERROR. Deleting it collapses a timeout
            # into a successful publish of an unprocessed container.
            return self._fail(
                account,
                "reel processing timed out after {0:.0f}s ({1} polls); the "
                "container may still finish, so check the account before "
                "re-posting".format(attempts * interval, attempts),
            )

    # -- helpers -------------------------------------------------------------- #
    def _is_video(self, url: str) -> bool:
        """Sniff the media type from the extension, query string stripped first.

        Public and signed URLs carry query parameters, and leaving them on makes
        every ``.mp4?X-Goog-Signature=...`` look like a photo.
        """
        clean = str(url).split("?")[0].lower()
        return clean.endswith(tuple(self.cfg.publish.media_type_from_extension))

    def _cover_offset(self, media_path: str) -> Optional[int]:
        """Cover offset in milliseconds, from the SHARED media helper.

        This is deliberately not a local function. It used to live in this module
        and the Facebook publisher imported it, while this module imported that
        one back for Page credentials — a circular import that only function-level
        imports kept from exploding. It is pure ffmpeg brightness scanning with no
        Instagram in it, so it belongs a layer down where both publishers reach it
        as equals.
        """
        if not self.cfg.publish.send_thumb_offset:
            return None
        if not self._is_video(media_path):
            return None
        return cover_offset_ms(
            media_path, tools=self.tools, config=self.config.media.cover
        )

    def _body_text(self, response: Any) -> str:
        text = getattr(response, "text", "") or ""
        return text[: self._error_chars]

    def _fail(self, account: str, reason: str) -> PublishResult:
        log.warning("instagram/%s: %s", account or "-", reason)
        return PublishResult(
            False, platform=self.platform, account=account, reason=reason
        )


def publisher(config: Any, **kwargs: Any) -> InstagramPublisher:
    """Factory used by :func:`makervox_publish.platforms.publisher_for`."""
    return InstagramPublisher(config, **kwargs)
