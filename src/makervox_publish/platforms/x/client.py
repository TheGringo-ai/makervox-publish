"""X (Twitter) publisher — API v2 ``POST /2/tweets``, OAuth 1.0a user context.

⚠️ THERE IS NO FREE TIER. This platform is pay-per-use on prepaid credits, and a
post CONTAINING A URL is billed at a different, much higher rate than a plain
one (roughly 13x at the time of writing — see ``platforms.x.pricing``, which
ships with no figures and a ``verified_on`` date precisely because prices rot).
That single fact drives three defaults in this module: the link lives in the
profile BIO rather than the post, the link reply is OFF, and the create call is
never retried.

Setup, once, on YOUR OWN developer app:

1. Create an app in the platform's developer console and buy credits.
2. Enable OAuth 1.0a with **Read+Write**, then REGENERATE the access token —
   a token minted before the permission change keeps the old, read-only scope
   and fails at post time with an error that does not say so.
3. Provide four secrets by name (defaults shown; every name is overridable in
   config so several apps can coexist in one environment)::

       X_API_KEY  X_API_SECRET  X_ACCESS_TOKEN  X_ACCESS_SECRET

Until all four resolve the client is INERT: it returns ``(False, reason)`` and
posts nothing, so wiring it into a publish flow before setup is a silent no-op
rather than a crash that takes the other platforms down with it.

Media: stills only. Video needs the chunked INIT/APPEND/FINALIZE flow plus
status polling, which is not implemented here — a vertical 60-second reel is not
native to this platform anyway. An image is a BONUS, never a blocker: a failed
upload must still let the text post go out rather than lose the day's slot over
decoration.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from makervox_publish.credentials import CredentialSpec, Credentials
from makervox_publish.http.client import HttpClient, HttpPolicy
from makervox_publish.logging import get_logger
from makervox_publish.platforms import PublishResult
from makervox_publish.platforms.x.governor import Governor
from makervox_publish.text.shape import shape_trace

__all__ = ["XClient", "X_CREDENTIALS", "credential_spec", "shape"]

log = get_logger(__name__)

#: Default credential NAMES, and the setup line shown when they are missing.
#: The names are defaults only — ``platforms.x.*_credential`` overrides each one.
X_CREDENTIALS = CredentialSpec(
    platform="x",
    keys=("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"),
    setup=(
        "Create YOUR OWN X developer app with OAuth 1.0a Read+Write, REGENERATE "
        "the access token after setting those permissions, and buy credits "
        "(there is no free tier)."
    ),
    caps=frozenset({"text", "image"}),
)


def credential_spec(x_config) -> CredentialSpec:
    """The spec for one configured app, honouring renamed credential keys."""
    return CredentialSpec(
        platform="x",
        keys=(
            x_config.api_key_credential,
            x_config.api_secret_credential,
            x_config.access_token_credential,
            x_config.access_secret_credential,
        ),
        setup=X_CREDENTIALS.setup,
        caps=X_CREDENTIALS.caps,
    )


def shape(caption: str, shape_config=None) -> str:
    """Shape a caption into a post body.

    Module-level, pure and credential-free so the rules can be tested without a
    network call — the part most likely to drift is the part hardest to see.
    """
    from makervox_publish.config.schema import XShapeConfig
    from makervox_publish.text.shape import shape_with_config

    return shape_with_config(caption, shape_config or XShapeConfig())


class XClient:
    """Publishes to one X account, under one governor.

    Every public method returns rather than raises. A scheduled rotation that is
    frozen by an exception escaping a publisher stops posting EVERYTHING, not
    just the platform that failed, so the boundary here is total.
    """

    platform = "x"

    def __init__(
        self,
        config,
        *,
        credentials: Optional[Credentials] = None,
        accounts: Optional[Mapping[str, Any]] = None,
        governor: Optional[Governor] = None,
        tagger: Optional[Any] = None,
        http: Optional[HttpClient] = None,
        program_name: str = "makervox_publish",
        error_body_chars: int = 400,
        logger=None,
    ) -> None:
        self.config = config
        self.credentials = credentials or Credentials.default()
        self.accounts = dict(accounts or {})
        self.spec = credential_spec(config)
        self.governor = governor if governor is not None else Governor.from_config(config)
        self.tagger = tagger
        self.http = http or HttpClient()
        self.program_name = program_name
        self.error_body_chars = int(error_body_chars)
        self._log = logger or log

    @classmethod
    def from_config(cls, cfg, *, credentials: Optional[Credentials] = None,
                    governor: Optional[Governor] = None,
                    http: Optional[HttpClient] = None) -> "XClient":
        """Build from a whole :class:`makervox_publish.config.Config`."""
        x_config = cfg.platforms.x
        creds = credentials or Credentials.from_config(cfg.credentials)
        tagger = None
        try:
            tagger = cfg.links.tagger.build(group="makervox_publish.link_taggers")
        except Exception as exc:  # noqa: BLE001 - a bad tagger must not block posting
            # Deliberate swallow, logged with the reason: an untagged link still
            # reaches the site, while refusing to post over a UTM template would
            # lose the day's slot. Attribution is the thing degraded, not reach.
            log.warning("links.tagger could not be built (%s); posting untagged links", exc)
        return cls(
            x_config,
            credentials=creds,
            accounts=cfg.accounts,
            governor=governor or Governor.from_config(x_config),
            tagger=tagger,
            http=http or HttpClient(HttpPolicy.from_config(cfg.http)),
            program_name=cfg.cli.program_name,
            error_body_chars=cfg.logging.provider_error_body_chars,
        )

    # ------------------------------------------------------------------ #
    # credentials and auth
    # ------------------------------------------------------------------ #
    def ready(self) -> Tuple[bool, List[str]]:
        """``(ready, missing_credential_names)``. Never raises, never logs values."""
        _, missing = self.credentials.require(self.spec.keys_required)
        return (not missing), missing

    def _auth(self) -> Tuple[Any, Optional[str]]:
        """Build the OAuth 1.0a signer. Returns ``(auth, error)``.

        ``requests_oauthlib`` is an OPTIONAL extra, not a hard dependency: the
        rest of the package must install and import without it.
        """
        values, missing = self.credentials.require(self.spec.keys_required)
        if missing:
            return None, self.spec.not_configured_reason(missing)
        try:
            from requests_oauthlib import OAuth1
        except ImportError:
            return None, (
                "requests-oauthlib is not installed — this platform needs OAuth "
                "1.0a signing. Install it with: pip install \"makervox-publish[x]\""
            )
        return OAuth1(
            values[self.config.api_key_credential],
            values[self.config.api_secret_credential],
            values[self.config.access_token_credential],
            values[self.config.access_secret_credential],
        ), None

    # ------------------------------------------------------------------ #
    # raw calls
    # ------------------------------------------------------------------ #
    def _error(self, response) -> str:
        try:
            body = (response.text or "")[: self.error_body_chars]
        except Exception:  # pragma: no cover - undecodable body
            body = "<unreadable body>"
        return "{0}: {1}".format(response.status_code, body)

    def upload_image(self, path: str, auth: Any) -> Tuple[Optional[str], Optional[str]]:
        """Upload one still image, returning ``(media_id, error)``.

        A single multipart POST to the LEGACY v1.1 media host — a real
        mixed-generation quirk of this API, not a typo: the post itself is v2
        while media upload is not.
        """
        if not self.config.media.image_upload_enabled:
            return None, "image upload is disabled (platforms.x.media.image_upload_enabled)"
        try:
            with open(path, "rb") as handle:
                response = self.http.post(
                    self.config.media_upload_url,
                    kind="media_create",
                    auth=auth,
                    files={"media": handle},
                )
        except Exception as exc:  # noqa: BLE001 - unreadable file, transport, anything
            return None, "read/upload failed: {0}".format(str(exc)[:120])
        if response.status_code in (200, 201):
            try:
                media_id = (response.json() or {}).get("media_id_string") or ""
            except ValueError:
                return None, "upload returned a non-JSON body"
            return (str(media_id) or None), None
        return None, self._error(response)

    def _tweet(self, text: str, auth: Any, *, reply_to: Optional[str] = None,
               media_ids: Optional[Sequence[str]] = None) -> Tuple[bool, str]:
        """Create one post. Returns ``(ok, id_or_error)``.

        ``attempts=1`` — NOT RETRIED, deliberately, and the transport case is
        the reason. Retrying an API rejection merely makes it worse, but a
        create that fails at the TRANSPORT layer may ALREADY have been committed
        on the far side: the request arrived, the answer did not come back. A
        retry there publishes twice and bills twice for one intended post. A
        missed post is recoverable on the next run; a duplicate is not, and a
        near-duplicate is reach-suppressed on top of the wasted money.
        """
        payload = {
            "text": (text or "").strip()[: self.config.shape.max_chars]
        }  # type: Dict[str, Any]
        if media_ids:
            payload["media"] = {"media_ids": [str(m) for m in media_ids]}
        if reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": str(reply_to)}
        try:
            response = self.http.post(
                self.config.api_base.rstrip("/") + "/tweets",
                kind="small_write",
                attempts=1,          # see the docstring — never retry a create
                auth=auth,
                json=payload,
            )
        except Exception as exc:  # noqa: BLE001 - transport; see the docstring
            return False, "request failed: {0}".format(str(exc)[:160])
        if response.status_code in (200, 201):
            try:
                data = (response.json() or {}).get("data") or {}
            except ValueError:
                data = {}
            return True, str(data.get("id", ""))
        return False, self._error(response)

    # ------------------------------------------------------------------ #
    # the simple path
    # ------------------------------------------------------------------ #
    def post(self, text: str, media_path: Optional[str] = None) -> PublishResult:
        """Post one body immediately, bypassing the governor.

        For a caller that has already decided. :meth:`auto_post` is the governed
        entry point and is what a scheduler should use.
        """
        auth, error = self._auth()
        if error:
            return PublishResult(False, platform=self.platform, reason=error)
        note = ""
        if media_path and self._is_video(media_path):
            # Stills only — see the module docstring. Say so in the result
            # rather than silently dropping the attachment.
            note = " (video not uploaded: stills only on this platform)"
        media_ids = None
        if media_path and not self._is_video(media_path) and os.path.isfile(media_path):
            media_id, media_error = self.upload_image(media_path, auth)
            if media_id:
                media_ids = [media_id]
            else:
                note = " (image skipped: {0})".format(str(media_error)[:80])
        ok, result = self._tweet(text, auth, media_ids=media_ids)
        if not ok:
            return PublishResult(False, platform=self.platform, reason=result)
        return PublishResult(True, platform=self.platform, post_id=result,
                             reason="posted {0}{1}".format(result, note).strip())

    @staticmethod
    def _is_video(path: str) -> bool:
        return os.path.splitext(path)[1].lower() in (".mp4", ".mov", ".m4v", ".webm")

    # ------------------------------------------------------------------ #
    # the governed path
    # ------------------------------------------------------------------ #
    def auto_post(
        self,
        account: str,
        caption: str,
        *,
        link: Optional[str] = None,
        slug: Optional[str] = None,
        image: Optional[str] = None,
        link_reply: Optional[bool] = None,
        dry_run: bool = False,
    ) -> PublishResult:
        """Post one governed update for ``account``. Returns ``(ok, detail)``.

        NEVER RAISES. A scheduled rotation frozen by an exception escaping one
        publisher stops posting every platform, not just this one, so every path
        below returns instead of propagating.

        ``link_reply`` defaults to the configured value, which is OFF on COST,
        not preference: the reply itself carries a URL and is billed at the
        expensive rate wherever the link sits.
        """
        try:
            return self._auto_post(
                account, caption, link=link, slug=slug, image=image,
                link_reply=link_reply, dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - the boundary; see the docstring
            self._log.exception("x: auto_post for %r failed", account)
            return PublishResult(
                False, platform=self.platform, account=account,
                reason="error: {0}: {1}".format(type(exc).__name__, str(exc)[:400]),
            )

    def _auto_post(
        self,
        account: str,
        caption: str,
        *,
        link: Optional[str],
        slug: Optional[str],
        image: Optional[str],
        link_reply: Optional[bool],
        dry_run: bool,
    ) -> PublishResult:
        def fail(detail: str) -> PublishResult:
            return PublishResult(False, platform=self.platform, account=account,
                                 reason=detail)

        if not getattr(self.config, "enabled", False):
            return fail("platforms.x.enabled is false")

        # 1. Allow-lists first: cheapest, most decisive, and an account that can
        #    never post should not trigger a secret lookup on the way to being
        #    told so.
        policy = self.governor.check_policy(account, slug)
        if not policy.allowed:
            return fail(policy.reason)

        # 2. Credentials. Missing ones make this platform INERT, not fatal.
        ready, missing = self.ready()
        if not ready:
            return fail(self.spec.not_configured_reason(missing))

        # 3. Shape. A stub is worse than nothing: it spends the daily slot and
        #    the money on a post that says nothing.
        trace = shape_trace(
            caption,
            max_chars=self.config.shape.max_chars,
            truncate_to=self.config.shape.truncate_to,
            min_sentence_boundary=self.config.shape.min_sentence_boundary,
            max_hashtags=self.config.shape.max_hashtags,
            strip_urls=self.config.shape.strip_urls,
            trailing_connectors=self.config.shape.trailing_connectors,
        )
        body = trace.final
        if len(body) < self.config.shape.min_body_chars:
            return fail("caption too thin after shaping ({0} chars, minimum {1})".format(
                len(body), self.config.shape.min_body_chars))

        # 4. Cap and near-duplicate, against the shaped body.
        decision = self.governor.check(account, body, slug)
        if not decision.allowed:
            return fail(decision.reason)

        want_reply = self.config.link_reply.enabled if link_reply is None else bool(link_reply)
        target = link or self._account_link(account)
        if want_reply and not target:
            # Refusing here rather than posting a reply with an empty URL.
            self._log.warning(
                "x: link reply requested for %r but no link_url is configured; "
                "posting without it", account,
            )
            want_reply = False

        if dry_run:
            return PublishResult(
                True, platform=self.platform, account=account,
                reason=self._dry_run_detail(body, want_reply),
                extra={"body": body, "link_reply": want_reply, "dry_run": True},
            )

        auth, auth_error = self._auth()
        if auth_error:
            return fail(auth_error)

        # 5. An image is a BONUS, never a blocker. A failed upload must still
        #    let the text go out rather than lose the day's slot over decoration.
        media_ids = None  # type: Optional[List[str]]
        media_note = ""
        if image and os.path.isfile(image):
            if self._is_video(image):
                media_note = " (video not uploaded: stills only on this platform)"
            else:
                media_id, media_error = self.upload_image(image, auth)
                if media_id:
                    media_ids, media_note = [media_id], " +image"
                else:
                    media_note = " (image skipped: {0})".format(str(media_error)[:80])
                    if self.config.media.image_failure_blocks_post:
                        return fail("image upload failed: {0}".format(media_error))
        elif image:
            media_note = " (image not found: {0})".format(os.path.basename(image))

        ok, result = self._tweet(body, auth, media_ids=media_ids)
        if not ok:
            return fail(result)
        post_id = result

        # 6. COUNT THE POST BEFORE THE OPTIONAL REPLY. The post is already live
        #    at this point, and a reply failure that skipped the counter would
        #    re-post the same text on the next run — paying twice to duplicate
        #    ourselves.
        self.governor.record(account, body)

        detail = "posted {0}{1}".format(post_id, media_note)
        if not want_reply:
            return PublishResult(True, platform=self.platform, account=account,
                                 post_id=post_id, reason=detail)

        # 7. Opt-in only: this reply CARRIES A URL and is therefore billed at the
        #    expensive rate. See platforms.x.link_reply and platforms.x.pricing.
        reply_text = self._link_reply_text(target, account)
        if not reply_text:
            return PublishResult(True, platform=self.platform, account=account,
                                 post_id=post_id, reason=detail)
        reply_ok, reply_result = self._tweet(reply_text, auth, reply_to=post_id)
        if not reply_ok:
            # The post is live; the reply is decoration. Report both, succeed.
            detail += " (link reply failed: {0})".format(str(reply_result)[:160])
        return PublishResult(True, platform=self.platform, account=account,
                             post_id=post_id, reason=detail)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _account_link(self, account: str) -> Optional[str]:
        entry = self.accounts.get(account)
        return getattr(entry, "link_url", None) if entry is not None else None

    def _link_reply_text(self, url: str, account: str) -> str:
        cfg = self.config.link_reply
        tagged = self._tag_url(url, account=account, medium=cfg.utm_medium)
        try:
            return cfg.template.format(cta=cfg.cta, url=tagged).strip()
        except (KeyError, IndexError, ValueError) as exc:
            # A template that names a placeholder we do not supply must not take
            # the post down with it — the post is already live by now.
            self._log.warning(
                "platforms.x.link_reply.template is not usable (%s); falling back "
                "to '<cta> <url>'", exc,
            )
            return "{0} {1}".format(cfg.cta, tagged).strip()

    def _tag_url(self, url: str, *, account: str, medium: str) -> str:
        """Apply the configured UTM tagger, if there is one.

        Duck-typed on purpose: a tagger may be a ``LinkTagger`` object or a plain
        callable supplied by a library caller. A tagger that fails returns the
        URL untouched — an untagged link still works, and losing the post to
        protect attribution would be the wrong trade.
        """
        tagger = self.tagger
        if tagger is None:
            return url
        context = {
            "platform": self.platform,
            "medium": medium,
            "account": account,
            "date": dt.date.today(),
        }
        for attribute in ("tag_url", "tag", "__call__"):
            method = getattr(tagger, attribute, None)
            if method is None:
                continue
            try:
                return str(method(url, **context))
            except TypeError:
                try:
                    return str(method(url))
                except Exception as exc:  # noqa: BLE001
                    self._log.warning("link tagger failed (%s); using the raw URL", exc)
                    return url
            except Exception as exc:  # noqa: BLE001
                self._log.warning("link tagger failed (%s); using the raw URL", exc)
                return url
        return url

    def _dry_run_detail(self, body: str, link_reply: bool) -> str:
        """Report the ACTUAL plan.

        This once read "+ link reply" unconditionally, which claimed the one
        action that costs an order of magnitude more on a path where it does not
        happen. A dry run that misstates the expensive branch is worse than no
        dry run at all.
        """
        pricing = self.config.pricing
        plan = "would post {0} chars{1}".format(
            len(body), " + link reply (URL)" if link_reply else ", no URL"
        )
        if not (pricing.price_per_post or pricing.price_per_post_with_url):
            return "dry-run ok — {0}; cost unknown (platforms.x.pricing is unset)".format(plan)
        cost = pricing.price_per_post + (
            pricing.price_per_post_with_url if link_reply else 0.0
        )
        stamp = " (prices verified {0})".format(pricing.verified_on) if pricing.verified_on \
            else " (prices carry no verified_on date — re-check them)"
        return "dry-run ok — {0} ~ {1:.3f} {2}{3}".format(
            plan, cost, pricing.currency, stamp
        )

    def estimate(self, *, posts: int = 1, link_reply: Optional[bool] = None) -> str:
        """Cost of ``posts`` posts under the current settings, for ``makervox_publish estimate``."""
        pricing = self.config.pricing
        with_url = self.config.link_reply.enabled if link_reply is None else bool(link_reply)
        if not (pricing.price_per_post or pricing.price_per_post_with_url):
            return ("platforms.x.pricing is unset, so no estimate can be given. "
                    "Fill it in from the current pricing page.")
        per_post = pricing.price_per_post + (
            pricing.price_per_post_with_url if with_url else 0.0
        )
        total = per_post * max(0, int(posts))
        return (
            "{0} post(s) {1} link reply: {2:.3f} {3} "
            "(cap {4}/day = {5:.2f} {3}/year at that rate)".format(
                posts, "with" if with_url else "without", total, pricing.currency,
                self.governor.config.daily_cap,
                per_post * self.governor.config.daily_cap * 365,
            )
        )

    def status(self) -> Dict[str, Any]:
        """A credential-free summary for ``makervox_publish doctor``."""
        ready, missing = self.ready()
        return {
            "platform": self.platform,
            "enabled": bool(getattr(self.config, "enabled", False)),
            "ready": ready,
            "missing": missing,
            "setup": self.spec.setup if missing else "",
            "daily_cap": self.governor.config.daily_cap,
            "posts_today": self.governor.posts_today(),
            "enabled_accounts": list(self.governor.config.enabled_accounts),
            "link_reply": self.config.link_reply.enabled,
            "governor": self.governor.describe(),
        }

    def __repr__(self) -> str:
        return "<XClient cap={0}/day accounts={1}>".format(
            self.governor.config.daily_cap, list(self.governor.config.enabled_accounts)
        )
