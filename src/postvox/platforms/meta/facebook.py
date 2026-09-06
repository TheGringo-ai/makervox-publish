"""Facebook Page publishing: reels, photos, text, Page metadata, reel metrics.

WHAT THIS MODULE IS
-------------------
The Graph API can only post to a PAGE, never to a personal profile. You bring
your own Meta developer app; one app can manage many Pages, and each local
account name here maps to one Page.

Page-token permissions, by feature:

===============================  ==========================================
``pages_show_list``,             posting (feed, photos, reels)
``pages_read_engagement``,
``pages_manage_posts``
``pages_manage_metadata``        setting the Page's about text / cover photo
``pages_manage_engagement``      creating comments (the first-comment CTA)
``read_insights``                reel play counts
===============================  ==========================================

A missing ``pages_manage_engagement`` returns ``(#200) insufficient
permissions`` on the COMMENT only; the reel still publishes. That asymmetry is
deliberate throughout: nothing optional is allowed to fail a post.

THE FIVE THINGS THAT MAKE THIS FILE WORTH READING
-------------------------------------------------
1. **One video id per reel.** The 3-phase resumable upload mints a video id in
   ``start``, and every retry of upload/finish reuses THAT id. Retrying the
   whole publish instead re-runs ``start``, mints a NEW id, and a network drop
   after the platform already committed produces a SECOND reel.
2. **The lock does not deduplicate.** It serializes. The delivery re-check
   therefore happens INSIDE the lock; a check made before acquiring it is a
   TOCTOU whose snapshot goes stale while blocked.
3. **A filename is not an identity.** Dedupe is on
   ``(account, date, content fingerprint)``, hashed from the SOURCE media, never
   from the temp cut that goes over the wire.
4. **The commentable object is not the post id.** It is
   ``{page_id}_{story_fbid}``, and both resolving it AND posting to it race.
5. **The auto-picked cover is often black.** Reels that fade in from black get a
   dark preferred thumbnail, and the grid renders as black tiles.

Each is spelled out at the code that implements it.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from postvox.credentials import Credentials
from postvox.errors import PublishError
from postvox.http.client import HttpClient, HttpPolicy
from postvox.identity.post_id import (
    FilenamePostIdentity,
    PostIdentity,
    PostIdentityResolver,
    resolve_identity,
)
from postvox.logging import get_logger, swallowed
from postvox.media.cover import cover_offset_ms
from postvox.media.ffmpeg import FfmpegTools
from postvox.media.shortcut import short_cut_for_publish
from postvox.platforms import PublishResult
from postvox.platforms.meta import compat
from postvox.platforms.meta.graph import GraphEndpoints, GraphSession
from postvox.platforms.meta.tokens import (
    MetaTokenStore,
    PageCredentials,
    PageResolver,
    StoredPageResolver,
)
from postvox.state.ledger import JsonlLedger, LedgerRow
from postvox.state.locks import file_lock
from postvox.text.links import NoopTagger, readable_text, strip_from_config

__all__ = ["FacebookPublisher", "ReelUploadSession", "publisher"]

log = get_logger(__name__)

PLATFORM = "facebook"


@dataclass(frozen=True)
class LockSpec:
    """Where the publish lock lives and what happens if it cannot be taken."""

    dir: str
    backend: str = "auto"
    timeout_s: float = 120.0
    on_unavailable: str = "proceed"

    @classmethod
    def from_config(cls, state_cfg) -> "LockSpec":
        locks = getattr(state_cfg, "locks", None)
        state_dir = os.path.expanduser(getattr(state_cfg, "dir", "."))
        return cls(
            dir=os.path.expanduser(
                getattr(locks, "dir", "") or os.path.join(state_dir, "locks")
            ),
            backend=getattr(locks, "backend", "auto"),
            timeout_s=float(getattr(locks, "acquire_timeout_s", 120.0)),
            on_unavailable=getattr(locks, "on_unavailable", "proceed"),
        )


class ReelUploadSession:
    """One reel = one ``video_id``, minted exactly once.

    THIS IS THE DUPLICATE-PREVENTION STORY, and it is an OBJECT rather than a
    code convention so a refactor cannot quietly re-mint the id.

    The reel upload has three phases: ``start`` mints a video id, the bytes go
    to the resumable-upload host, and ``finish`` publishes. The transient-network
    retry belongs around phases two and three ONLY:

    * re-sending the bytes to the same video id is idempotent — the upload host
      accepts a re-send at offset 0, so a timeout whose request actually landed
      is deduped, not duplicated;
    * re-finishing the same video id returns the existing post, or an "already
      published" error that is tolerated rather than treated as a failure;
    * re-running ``start`` mints a NEW video id, and if the platform had already
      committed the first one, that is a second reel on the Page. Which is
      exactly what an outer retry loop used to do.
    """

    def __init__(self, graph: GraphSession, page: PageCredentials, *,
                 retries: int = 3, backoff_s: float = 3.0,
                 tolerate_already_published: bool = True) -> None:
        self.graph = graph
        self.page = page
        self.retries = max(1, int(retries))
        self.backoff_s = float(backoff_s)
        self.tolerate_already_published = bool(tolerate_already_published)
        self.video_id = ""

    # -- phase 1: mint the id, ONCE ------------------------------------------- #
    def start(self) -> str:
        if self.video_id:
            # Not an optimization: calling start() twice for one reel is the bug
            # this class exists to make impossible.
            return self.video_id
        payload = self.graph.post(
            "{0}/video_reels".format(self.page.page_id), self.page.token,
            {"upload_phase": "start"}, kind="media_create", attempts=1,
        )
        video_id = str(payload.get("video_id") or "")
        if not video_id:
            raise PublishError("reel start failed: {0}".format(self.graph.error(payload)))
        self.video_id = video_id
        return video_id

    # -- phase 2: the bytes ---------------------------------------------------- #
    def upload(self, video_path: str) -> Dict[str, Any]:
        if not self.video_id:
            raise PublishError("upload() called before start(); no video id exists")
        size = os.path.getsize(video_path)

        def _send() -> Dict[str, Any]:
            with open(video_path, "rb") as handle:
                payload = self.graph.upload_bytes(
                    self.video_id, self.page.token, handle.read(),
                    offset=0, file_size=size,
                    # Safe to retry: same bytes, same video id, offset 0.
                    attempts=self.retries,
                )
            if not compat.upload_succeeded(payload):
                raise PublishError("reel upload failed: {0}".format(
                    self.graph.error(payload)
                ))
            return payload

        return self._retry("upload", _send)

    # -- phase 3: finish + publish -------------------------------------------- #
    def finish(self, description: str = "") -> Dict[str, Any]:
        if not self.video_id:
            raise PublishError("finish() called before start(); no video id exists")

        def _finish() -> Dict[str, Any]:
            payload = self.graph.post(
                "{0}/video_reels".format(self.page.page_id), self.page.token,
                {
                    "upload_phase": "finish",
                    "video_id": self.video_id,
                    "video_state": "PUBLISHED",
                    "description": description,
                },
                kind="media_create",
                # Idempotent by construction: same video id every time.
                attempts=self.retries,
            )
            if payload.get("success") or "post_id" in payload:
                return payload
            if self.tolerate_already_published and compat.is_already_published(payload):
                # A prior attempt committed and its response was lost. The reel
                # IS on the Page; re-finishing is not an error.
                log.info("reel %s was already published on a previous attempt",
                         self.video_id)
                return payload
            raise PublishError("reel finish failed: {0}".format(self.graph.error(payload)))

        return self._retry("finish", _finish)

    # -- shared retry ---------------------------------------------------------- #
    def _retry(self, what: str, fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        """Retry ``fn`` against the SAME video id.

        Only transport failures reach here as retryable; a PublishError is an
        API rejection and is raised immediately, because re-sending a rejected
        request makes the rejection worse.
        """
        last = None  # type: Optional[BaseException]
        for attempt in range(1, self.retries + 1):
            try:
                return fn()
            except PublishError:
                raise
            except Exception as exc:
                last = exc
                if attempt >= self.retries:
                    break
                log.warning(
                    "reel %s on video_id %s: %s — retry %d/%d in %.0fs",
                    what, self.video_id, exc, attempt, self.retries - 1,
                    self.backoff_s * attempt,
                )
                time.sleep(self.backoff_s * attempt)
        raise PublishError("reel {0} failed on video_id {1}: {2}".format(
            what, self.video_id, last
        ))


class FacebookPublisher:
    """Publish to one Meta app's Pages.

    Construct with :meth:`from_config`; the explicit ``__init__`` exists so a
    test can inject a fake Graph session, ledger or clock without a config file.
    """

    def __init__(
        self,
        graph: GraphSession,
        resolver: PageResolver,
        *,
        tokens: Optional[MetaTokenStore] = None,
        reels_cfg=None,
        dedupe_cfg=None,
        strip_cfg=None,
        cover_cfg=None,
        short_cut_cfg=None,
        ledger=None,
        identity_resolver: Optional[PostIdentityResolver] = None,
        on_unresolved: str = "warn_and_publish",
        link_tagger: Optional[Callable[..., str]] = None,
        first_comments: Optional[Dict[str, str]] = None,
        lock: Optional[LockSpec] = None,
        tools: Optional[FfmpegTools] = None,
    ) -> None:
        self.graph = graph
        self.resolver = resolver
        self.tokens = tokens
        self.reels = reels_cfg
        self.dedupe = dedupe_cfg
        self.strip_cfg = strip_cfg
        self.cover_cfg = cover_cfg
        self.short_cut_cfg = short_cut_cfg
        self.ledger = ledger
        self.identity_resolver = identity_resolver
        self.on_unresolved = on_unresolved
        self.link_tagger = link_tagger or NoopTagger()
        #: account -> call-to-action text. EMPTY BY DEFAULT: the package ships
        #: no marketing copy and no account names.
        self.first_comments = dict(first_comments or {})
        self.lock = lock
        self.tools = tools or FfmpegTools()

    # ---------------------------------------------------------------- factory #
    @classmethod
    def from_config(cls, config, *, credentials: Optional[Credentials] = None,
                    http: Optional[HttpClient] = None,
                    tokens: Optional[MetaTokenStore] = None,
                    resolver: Optional[PageResolver] = None,
                    ledger=None, tools: Optional[FfmpegTools] = None
                    ) -> "FacebookPublisher":
        """Wire everything from a :class:`postvox.config.Config`."""
        facebook = config.platforms.facebook
        store = tokens or MetaTokenStore.from_config(
            config, store_name=facebook.tokens_store
        )
        client = http or HttpClient(HttpPolicy.from_config(config.http))
        graph = GraphSession(
            client,
            GraphEndpoints.from_config(facebook),
            error_chars=int(getattr(config.logging, "provider_error_body_chars", 400)),
        )

        tagger = None
        spec = getattr(config.links, "tagger", None)
        if spec is not None:
            try:
                tagger = spec.build(group="postvox.link_taggers")
            except Exception as exc:
                # A broken tagger must not stop publishing; an UNTAGGED link is
                # a reporting gap, a failed publish is a missed post.
                swallowed(log, "building links.tagger", exc,
                          detail="links will be published untagged")

        return cls(
            graph,
            resolver or StoredPageResolver.from_config(config, store),
            tokens=store,
            reels_cfg=facebook.reels,
            dedupe_cfg=facebook.dedupe,
            strip_cfg=config.links.strip,
            cover_cfg=config.media.cover,
            short_cut_cfg=config.media.short_cut,
            ledger=ledger or JsonlLedger.from_config(config.ledger, config.state),
            identity_resolver=FilenamePostIdentity.from_config(config.identity),
            on_unresolved=getattr(config.identity, "on_unresolved", "warn_and_publish"),
            link_tagger=tagger,
            first_comments={
                name: account.first_comment
                for name, account in config.accounts.items()
                if account.first_comment
            },
            lock=LockSpec.from_config(config.state),
            tools=tools or FfmpegTools.from_config(config.media),
        )

    # -- config access that tolerates a hand-built publisher ------------------ #
    @property
    def _fc(self):
        """The ``[platforms.facebook.reels.first_comment]`` block, or None.

        Every read of it goes through ``getattr(..., default)`` so a publisher
        constructed by hand in a test does not have to supply the whole config
        tree just to post one reel.
        """
        return getattr(self.reels, "first_comment", None)

    # ------------------------------------------------------------ page access #
    def page(self, account: str) -> PageCredentials:
        """Page credentials for ``account``, or raise.

        The resolver returns None ONLY for "no Page linked". Anything else
        raises out of the resolver itself — a broken lookup must be loud, not a
        silent downgrade that reads exactly like an unconfigured account.
        """
        page = self.resolver.resolve(account)
        if page is None:
            raise PublishError(
                "no Facebook Page is linked for account {0!r}. Authorize the "
                "Page (`postvox auth facebook {0}`) or set "
                "accounts.{0}.facebook.page_id.".format(account)
            )
        return page

    def linked(self, account: str) -> bool:
        """Whether this account can publish, WITHOUT raising."""
        try:
            return self.resolver.resolve(account) is not None
        except Exception as exc:
            # The resolver blew up. That is not "not linked" — say which it is.
            log.error("Page lookup for account %r failed: %s", account, exc)
            return False

    # ----------------------------------------------------------- authorization #
    def connect(self, user_token: str, credentials: Credentials,
                app_id_key: str = "META_APP_ID",
                app_secret_key: str = "META_APP_SECRET") -> List[Dict[str, Any]]:
        """Exchange a user token for Page tokens and store them.

        A user token is short-lived; exchanging it yields a long-lived user
        token, and the Page tokens minted from THAT are non-expiring. Storing
        the Page tokens is the whole point — nothing here ever needs the user
        token again.

        Credentials come from the provider chain, never from a config file: the
        config file names secrets, it does not hold them.
        """
        if self.tokens is None:
            raise PublishError("connect() needs a token store; construct with tokens=")
        values, missing = credentials.require([app_id_key, app_secret_key])
        if missing:
            raise PublishError(
                "Meta app credentials are not configured: missing {0}. Create "
                "YOUR OWN app at developers.facebook.com, then export those "
                "names (or add a credential provider).".format(", ".join(missing))
            )

        exchanged = self.graph.get(
            "oauth/access_token", "", {
                "grant_type": "fb_exchange_token",
                "client_id": values[app_id_key],
                "client_secret": values[app_secret_key],
                "fb_exchange_token": user_token,
            }, kind="auth",
        )
        long_token = exchanged.get("access_token")
        if not long_token:
            raise PublishError("long-lived token exchange failed: {0}".format(
                self.graph.error(exchanged)
            ))

        accounts = self.graph.get(
            "me/accounts", long_token, {"fields": "name,id,access_token"}, kind="auth"
        )
        pages = [p for p in (accounts.get("data") or []) if isinstance(p, dict)]
        if not pages:
            raise PublishError(
                "no Pages were returned. The token needs pages_show_list AND you "
                "must be an admin of at least one Page: {0}".format(
                    self.graph.error(accounts)
                )
            )

        # Read-modify-write on a file the Instagram side also writes: do it
        # inside the store's transaction so a token the other publisher just
        # refreshed is not silently dropped.
        with self.tokens.transaction() as txn:
            data = self.tokens.normalize(txn.data)
            for page in pages:
                data["pages"][str(page["id"])] = {
                    "name": page.get("name", ""),
                    "token": page["access_token"],
                    "ts": time.time(),
                }
            txn.save(data)
        return [{"id": p["id"], "name": p.get("name", "")} for p in pages]

    def link(self, account: str, page_id: str) -> str:
        """Map a local account name to a connected Page. Returns the Page name."""
        if self.tokens is None:
            raise PublishError("link() needs a token store; construct with tokens=")
        with self.tokens.transaction() as txn:
            data = self.tokens.normalize(txn.data)
            record = data["pages"].get(str(page_id))
            if not record:
                raise PublishError(
                    "Page {0} is not connected yet — run `postvox auth facebook` "
                    "first, then link it.".format(page_id)
                )
            # CROSS-ACCOUNT GUARD: a Page already claimed by another local name
            # means the wrong browser session authorized it, and every later post
            # would land on the wrong Page.
            for other, existing in data["accounts"].items():
                if str(existing) == str(page_id) and other != account:
                    raise PublishError(
                        "Page {0} is already linked to the account {1!r}. "
                        "Unlink it there first, or you will publish {2!r}'s "
                        "content to {1!r}'s Page.".format(page_id, other, account)
                    )
            data["accounts"][account] = str(page_id)
            data.pop("brands", None)      # migrate the v1 key on first write
            txn.save(data)
            return str(record.get("name") or "")

    def pages(self) -> Dict[str, str]:
        """``{page_id: name}`` for every connected Page."""
        if self.tokens is None:
            return {}
        document = self.tokens.document()
        return {
            pid: str((rec or {}).get("name") or "")
            for pid, rec in document["pages"].items()
        }

    # -------------------------------------------------------------- simple posts #
    def post_text(self, account: str, message: str) -> str:
        page = self.page(account)
        payload = self.graph.post("{0}/feed".format(page.page_id), page.token,
                                  {"message": message})
        if "id" not in payload:
            raise PublishError("text post failed: {0}".format(self.graph.error(payload)))
        return str(payload["id"])

    def post_photo(self, account: str, image_path: str, caption: str = "") -> str:
        page = self.page(account)
        with open(os.path.expanduser(image_path), "rb") as handle:
            payload = self.graph.post(
                "{0}/photos".format(page.page_id), page.token, {"caption": caption},
                files={"source": handle}, kind="media_create",
            )
        # The response carries `id` (the photo) and usually `post_id` (the
        # story). Either alone is success; post_id is the more useful one.
        post_id = compat.photo_post_id(payload)
        if not post_id:
            raise PublishError("photo post failed: {0}".format(self.graph.error(payload)))
        return post_id

    def comment(self, account: str, object_id: str, message: str) -> str:
        """Comment on one of this account's own posts (the first-comment CTA)."""
        page = self.page(account)
        payload = self.graph.post("{0}/comments".format(object_id), page.token,
                                  {"message": message})
        if "id" not in payload:
            raise PublishError("comment failed: {0}".format(self.graph.error(payload)))
        return str(payload["id"])

    # ------------------------------------------------------------------- reels #
    def post_reel(self, account: str, video_path: str, caption: str = "",
                  retries: Optional[int] = None) -> Tuple[str, str]:
        """Publish a reel. Returns ``(video_id, post_id)``.

        The retry lives INSIDE this call, in :class:`ReelUploadSession`, and
        reuses one video id. Do NOT wrap this function in a retry loop: that is
        precisely how a network drop after a committed publish produced a second
        reel.
        """
        page = self.page(account)
        path = os.path.abspath(os.path.expanduser(video_path))
        session = ReelUploadSession(
            self.graph, page,
            retries=int(retries if retries is not None
                        else getattr(self.reels, "upload_retries", 3)),
            backoff_s=float(getattr(self.reels, "upload_backoff_s", 3.0)),
            tolerate_already_published=bool(
                getattr(self.reels, "tolerate_already_published", True)
            ),
        )
        video_id = session.start()
        session.upload(path)
        finished = session.finish(caption)

        if getattr(self.reels, "set_cover_after_publish", True):
            # AFTER the publish, and never blocking it. This platform accepts a
            # new preferred thumbnail on an EXISTING video (unlike Instagram,
            # where the cover can only be set at publish time), so there is
            # nothing to gain from doing it earlier and a whole post to lose.
            try:
                self.set_reel_cover(video_id, path, page.token)
            except Exception as exc:
                swallowed(log, "reel cover for video_id {0}".format(video_id), exc,
                          detail="the platform's auto-picked frame stands")

        return video_id, str(finished.get("post_id") or "")

    def set_reel_cover(self, video_id: str, video_path: str, token: str) -> bool:
        """Force a non-black preferred thumbnail.

        WHY: reels that fade in from black start at brightness 0.0, and the
        platform picks an early frame. Measured on a live Page: eleven
        auto-generated thumbnails, and the DARKEST (brightness 1.6) was marked
        preferred while a 16.2 sat unused — 42 of 50 reels rendered as black
        tiles in the grid.

        The frame choice itself is :func:`postvox.media.cover.cover_offset_ms`,
        which lives in the media layer because the Instagram publisher needs the
        same thing. Keeping it inside one publisher and importing it from the
        other is what created the circular import in the original.
        """
        offset_ms = cover_offset_ms(video_path, tools=self.tools, config=self.cover_cfg)
        if not offset_ms:
            return False
        if not self.tools.ffmpeg:
            log.warning("cannot extract a cover frame for %s: ffmpeg is unavailable",
                        video_id)
            return False

        # mkstemp, not mktemp: mktemp returns a name and races anything that
        # creates it first.
        fd, frame = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            self.tools.run(
                ["-y", "-ss", "{0:.3f}".format(offset_ms / 1000.0), "-i", video_path,
                 "-frames:v", "1", "-q:v", "3", frame],
                timeout_s=60.0, binary="ffmpeg",
            )
            if not (os.path.exists(frame) and os.path.getsize(frame)):
                return False
            with open(frame, "rb") as handle:
                payload = self.graph.post(
                    "{0}/thumbnails".format(video_id), token,
                    {"is_preferred": "true"}, files={"source": handle},
                    kind="media_create",
                )
            # This endpoint answers {"success": true} and does NOT return an id,
            # so checking only for an id rejects every successful call.
            if not compat.thumbnail_accepted(payload):
                log.warning("cover rejected for video_id %s: %s", video_id,
                            self.graph.error(payload))
                return False
            return True
        finally:
            try:
                os.unlink(frame)
            except OSError:
                pass

    def reel_comment_target(self, account: str, video_id: str,
                            attempts: Optional[int] = None,
                            backoff_s: Optional[float] = None) -> Optional[str]:
        """Resolve the COMMENTABLE page-story object for a fresh reel.

        The finish phase's ``post_id`` is NOT commentable: posting to
        ``{page_id}_{video_id}``, or to the bare video id, returns
        ``(#100) Unsupported post request ... Object ... does not exist``. The
        real object is ``{page_id}_{story_fbid}``, where the story fbid is the
        VIDEO NODE'S own ``post_id`` field. Right after publishing it can lag a
        beat, so this retries briefly.
        """
        page = self.page(account)
        total = int(attempts if attempts is not None
                    else getattr(self._fc, "target_resolve_attempts", 5))
        pause = float(backoff_s if backoff_s is not None
                      else getattr(self._fc, "target_resolve_backoff_s", 3.0))
        for attempt in range(1, max(1, total) + 1):
            node = self.graph.get(str(video_id), page.token, {"fields": "post_id"},
                                  kind="poll")
            target = compat.story_object_id(page.page_id, node)
            if target:
                return target
            if attempt < total:
                time.sleep(pause * attempt)
        return None

    def add_first_comment(self, account: str, video_id: str, message: str) -> Optional[str]:
        """Post the first comment on a reel, retrying the POST itself.

        THE RACE: the story object EXISTS before it will accept comments. The
        ``post_id`` field appears on the video node almost immediately, but
        POSTing to ``{page_id}_{story}/comments`` right then still returns
        ``(#100) Unsupported post request``. Resolving the id is NOT enough —
        the POST has to be retried.

        Measured: the CTA had landed on only 7 of 288 reels, and every success
        was a run where the id resolution happened to be slow, so the resolver's
        own sleeps let the object settle. Backfilling the same day's failed reels
        through this exact path succeeded once time had passed, which is what
        pinned it as a race rather than a permission problem.

        Never raises. A missing comment is not worth failing a published post,
        and a missing ``pages_manage_engagement`` permission surfaces here as an
        insufficient-permissions error while the reel itself is fine.
        """
        target = self.reel_comment_target(account, video_id)
        if not target:
            log.warning("first comment skipped for video_id %s: the reel's story "
                        "object never resolved", video_id)
            return None

        total = int(getattr(self._fc, "post_attempts", 5))
        pause = float(getattr(self._fc, "post_backoff_s", 5.0))
        last = None  # type: Optional[BaseException]
        for attempt in range(1, max(1, total) + 1):
            try:
                return self.comment(account, target, message)
            except Exception as exc:
                last = exc
                if attempt < total:
                    # 5+10+15+20 = 50s, which covers the observed lag.
                    time.sleep(pause * attempt)
        log.warning(
            "first comment not added to video_id %s after %d attempts: %s. If "
            "this says insufficient permissions, the Page token is missing "
            "pages_manage_engagement — the reel itself published fine.",
            video_id, total, last,
        )
        return None

    # --------------------------------------------------------------- auto_post #
    def auto_post(
        self,
        account: str,
        video_path: str,
        caption: str = "",
        *,
        source_path: Optional[str] = None,
        first_comment: Optional[str] = None,
        identity: Optional[PostIdentity] = None,
        retries: Optional[int] = None,
        use_short_cut: Optional[bool] = None,
    ) -> PublishResult:
        """Publish a reel with dedupe, the link-free description and the CTA.

        SAFE BY CONTRACT: this never raises. An unlinked account, a platform
        rejection, an unreadable file — all come back as ``ok=False`` with a
        reason, so one dark account cannot abort a batch that also contains
        three working ones.

        ``source_path`` is the file that IDENTIFIES this post, and it defaults
        to ``video_path``. Pass the original dated file whenever ``video_path``
        is a throwaway temp cut: the ledger row is what a recovery job matches
        against, and a temp name (which carries no date) reads as "never
        delivered" and gets the whole day re-posted.
        """
        source = os.path.abspath(os.path.expanduser(source_path or video_path))
        try:
            if not self.linked(account):
                return PublishResult(False, PLATFORM, account, reason="not linked")

            cut_enabled = (
                bool(getattr(self.reels, "use_short_cut", False))
                if use_short_cut is None else bool(use_short_cut)
            )
            if cut_enabled:
                with short_cut_for_publish(video_path, self.tools,
                                           self.short_cut_cfg) as cut:
                    # The LEDGER still records `source`, not cut.path.
                    return self._guarded_publish(
                        account, cut.path, caption, source, first_comment,
                        identity, retries,
                    )
            return self._guarded_publish(
                account, video_path, caption, source, first_comment, identity, retries,
            )
        except Exception as exc:  # the never-raises contract
            log.error("auto_post failed for %s/%s: %s", account,
                      os.path.basename(source), exc)
            return PublishResult(False, PLATFORM, account, reason=str(exc)[:200])

    def _guarded_publish(
        self,
        account: str,
        wire_path: str,
        caption: str,
        source_path: str,
        first_comment: Optional[str],
        identity: Optional[PostIdentity],
        retries: Optional[int],
    ) -> PublishResult:
        """lock -> identity -> ledger RE-CHECK -> send -> record.

        The order is the whole point. The per-account lock only SERIALIZES
        concurrent callers; it does NOT deduplicate. Caller #2 waits for caller
        #1 to finish publishing and then publishes the same reel again — which
        is how one reel went out three times when a scheduled job, a health
        retry and a recovery pass all fired for it. So the delivery re-check
        happens INSIDE the lock: any check made before acquiring it is a TOCTOU
        whose snapshot goes stale while blocked.
        """
        lock_name = "{0}-publish-{1}".format(PLATFORM, account)
        with self._publish_lock(lock_name) as held:
            if not held:
                log.warning(
                    "publishing %s for %r WITHOUT the publish lock; a concurrent "
                    "run could interleave with this one",
                    os.path.basename(source_path), account,
                )

            post_identity = resolve_identity(
                self.identity_resolver, source_path, account,
                identity=identity, on_unresolved=self.on_unresolved,
                source_path=source_path,
            )

            duplicate = self._already_delivered(account, post_identity)
            if duplicate is not None:
                # ok=True on purpose: the reel IS on the Page, which is what
                # both callers were asking for. A near-duplicate upload gets
                # reach-suppressed, so a duplicate does not merely waste an
                # upload — it burns the slot.
                log.info(
                    "identical reel already delivered for %s on %s (media_id=%s) "
                    "— skipping the upload", account, duplicate.date,
                    duplicate.media_id,
                )
                return PublishResult(
                    bool(getattr(self.dedupe, "duplicate_result_is_ok", True)),
                    PLATFORM, account, media_id=duplicate.media_id,
                    post_id=duplicate.post_id, reason="already delivered",
                    duplicate=True,
                )

            # The DESCRIPTION goes link-free: an outbound link in the post body
            # throttles reach on this platform. The CTA rides in the first
            # comment instead.
            description = caption
            if getattr(self.reels, "strip_links_from_description", True):
                description = strip_from_config(caption, self.strip_cfg)

            video_id, post_id = self.post_reel(account, wire_path, description,
                                               retries=retries)
            self._record(account, video_id, post_id, post_identity, source_path)

        # OUTSIDE the lock: the reel is delivered and recorded, and the comment
        # race below takes up to a minute. Holding the publish lock through it
        # would block the next slot for no benefit.
        self._maybe_first_comment(account, video_id, caption, first_comment)
        return PublishResult(True, PLATFORM, account, media_id=video_id,
                             post_id=post_id)

    def _publish_lock(self, name: str):
        """The per-account publish lock, or a no-op when none is configured."""
        if self.lock is None:
            @contextlib.contextmanager
            def _unlocked():
                log.warning("no lock directory configured — publishing UNLOCKED")
                yield False
            return _unlocked()

        @contextlib.contextmanager
        def _locked():
            with file_lock(name, self.lock.dir, backend=self.lock.backend,
                           timeout_s=self.lock.timeout_s,
                           on_unavailable=self.lock.on_unavailable) as handle:
                yield bool(getattr(handle, "held", False))
        return _locked()

    def _already_delivered(self, account: str,
                           identity: Optional[PostIdentity]) -> Optional[LedgerRow]:
        """The ledger row this exact content was already delivered under.

        FAILS OPEN. No fingerprint, no date, or a pre-fingerprint (v1) ledger row
        all mean "cannot prove this is the same post", and the cost of a rare
        duplicate is far below the cost of silently suppressing a real one.
        """
        if self.ledger is None or identity is None or not identity.resolved:
            return None
        try:
            return self.ledger.find_delivered(account, identity.date, identity.sha,
                                              platform=PLATFORM)
        except Exception as exc:
            swallowed(log, "ledger delivery check", exc,
                      detail="publishing anyway (dedupe fails OPEN)")
            return None

    def _record(self, account: str, video_id: str, post_id: str,
                identity: Optional[PostIdentity], source_path: str) -> None:
        """Append the delivery row.

        The row names the SOURCE file, never the temp cut that went over the
        wire: recovery tooling matches on this row, and a temp filename with no
        date in it reads as "never delivered".
        """
        if self.ledger is None:
            return
        base = os.path.basename(source_path)
        try:
            self.ledger.append(LedgerRow(
                account=account,
                platform=PLATFORM,
                media_id=video_id,
                post_id=post_id,
                base=base,
                date=identity.date if identity else "",
                key=identity.key if identity else "",
                slot=identity.slot if identity else 1,
                sha=identity.sha if identity else "",
            ))
        except Exception as exc:
            # The post is live. A ledger write that failed must be LOUD — the
            # next run will see no row and publish it again.
            log.error(
                "reel %s was published for %s but the delivery row could NOT be "
                "written (%s). The next run will not know it went out.",
                video_id, account, exc,
            )

    def _maybe_first_comment(self, account: str, video_id: str, caption: str,
                             override: Optional[str]) -> None:
        """ONE first comment carrying the readable body AND the tagged CTA.

        One comment, not two posts: a first comment does not suppress reach the
        way an in-body link does, and folding the body in here avoids the
        near-duplicate text post that used to sit in the feed beside the reel.
        The body is link-stripped so the ONLY link in the comment is the single
        tagged call to action.
        """
        if not video_id or not getattr(self._fc, "enabled", True):
            return

        cta = override if override is not None else self.first_comments.get(account, "")
        if cta:
            try:
                cta = self.link_tagger(
                    cta,
                    platform=PLATFORM,
                    medium=getattr(self._fc, "utm_medium", "reel"),
                    account=account,
                    date=datetime.date.today(),
                )
            except Exception as exc:
                swallowed(log, "tagging the CTA link", exc,
                          detail="posting it untagged")

        parts = []
        if getattr(self._fc, "include_body", True):
            body = strip_from_config(readable_text(caption), self.strip_cfg)
            if body:
                parts.append(body)
        if cta:
            parts.append(cta)
        message = "\n\n".join(parts)
        if not message:
            return
        self.add_first_comment(account, video_id, message)

    # -------------------------------------------------------------- reporting #
    def posts(self, account: Optional[str] = None) -> List[LedgerRow]:
        """Every reel this package has delivered, for metric pulls."""
        if self.ledger is None:
            return []
        return self.ledger.rows(account=account, platform=PLATFORM)

    def video_metrics(self, account: str, video_id: str) -> Dict[str, int]:
        """Live numbers for a posted reel: plays, likes, comments.

        Best effort — returns only what the API gives. Reels have NO
        impressions/reach metric in this API version; plays are the proxy, and
        they come from ``/video_insights`` (which needs ``read_insights``), not
        from the ``views`` field on the video node. The two sources are combined
        with ``max`` because different videos populate different metric names.
        """
        page = self.page(account)
        out = {}  # type: Dict[str, int]
        node = self.graph.get(
            str(video_id), page.token,
            {"fields": "likes.summary(true),comments.summary(true),views"},
            kind="insights",
        )
        likes = ((node.get("likes") or {}).get("summary") or {})
        comments = ((node.get("comments") or {}).get("summary") or {})
        if "total_count" in likes:
            out["likes"] = int(likes["total_count"])
        if "total_count" in comments:
            out["comments"] = int(comments["total_count"])
        if node.get("views") is not None:
            try:
                out["views"] = int(node["views"])
            except (TypeError, ValueError):
                pass

        try:
            metrics = tuple(getattr(self.reels, "insight_metrics",
                                    ("blue_reels_play_count", "fb_reels_total_plays")))
            insights = self.graph.get(
                "{0}/video_insights".format(video_id), page.token,
                {"metric": ",".join(metrics)}, kind="insights",
            )
            plays = compat.insight_value(insights, metrics)
            if plays is not None:
                out["views"] = max(out.get("views", 0), plays)
        except Exception as exc:
            swallowed(log, "reel insights for {0}".format(video_id), exc,
                      detail="reporting whatever the video node gave")
        return out

    # --------------------------------------------------------- page management #
    def set_about(self, account: str, about: str = "",
                  description: str = "") -> Dict[str, Any]:
        """Set the Page's short 'about' and long 'description'.

        Needs ``pages_manage_metadata`` on the Page token — a permission the
        posting scopes do not include.
        """
        page = self.page(account)
        fields = {}  # type: Dict[str, Any]
        if about:
            fields["about"] = about
        if description:
            fields["description"] = description
        if not fields:
            return {}
        return self.graph.post(page.page_id, page.token, fields)

    def set_cover(self, account: str, image_path: str) -> Dict[str, Any]:
        """Set the Page's cover photo.

        Two steps, and they cannot be collapsed: upload the photo UNPUBLISHED
        (so it does not appear in the feed as a post), then point the Page's
        ``cover`` field at the resulting photo id.
        """
        page = self.page(account)
        with open(os.path.expanduser(image_path), "rb") as handle:
            uploaded = self.graph.post(
                "{0}/photos".format(page.page_id), page.token, {"published": "false"},
                files={"source": handle}, kind="media_create",
            )
        photo_id = uploaded.get("id")
        if not photo_id:
            raise PublishError("cover upload failed: {0}".format(
                self.graph.error(uploaded)
            ))
        result = self.graph.post(page.page_id, page.token, {"cover": photo_id})
        return {"photo_id": photo_id, "result": result}

    def set_picture(self, account: str, image_path: str) -> Dict[str, Any]:
        """Set the Page's profile picture."""
        page = self.page(account)
        with open(os.path.expanduser(image_path), "rb") as handle:
            return self.graph.post(
                "{0}/picture".format(page.page_id), page.token,
                files={"source": handle}, kind="media_create",
            )

    def describe(self) -> str:
        return "facebook via {0}".format(self.graph.describe())


def publisher(config, **kwargs) -> FacebookPublisher:
    """Entry point used by :func:`postvox.platforms.publisher_for`."""
    return FacebookPublisher.from_config(config, **kwargs)
