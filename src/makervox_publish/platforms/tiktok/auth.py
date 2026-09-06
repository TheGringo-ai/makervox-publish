"""TikTok OAuth: authorization, token exchange, and the refresh-rotation defence.

⚠️ SCOPES ARE FIXED AT AUTHORIZATION
------------------------------------
Refreshing a token does NOT widen its scopes. Changing ``scopes`` in config does
nothing at all until each account RE-AUTHORIZES. That is why a scope error
raises :class:`~makervox_publish.errors.ReauthorizationRequired` with an explicit
"re-auth, do not retry" message: the platform's own error text does not say so,
and "scope_not_authorized" reads like something a retry might fix.

Two scopes are routinely confused, and the difference is an ERROR rather than a
null field:

``user.info.basic``
    IDENTITY ONLY — open_id, display name, avatar. Ask it for ``follower_count``
    and the API returns an error.
``user.info.stats``
    the follower/following/likes/video counters.

``video.list`` is separate again, and it is what makes per-post performance
readable from the API instead of by hand-exporting from the creator tools.

⚠️ THE REFRESH TOKEN ROTATES ON USE
-----------------------------------
A successful refresh SPENDS the old refresh token. Two refreshes of one account
race destructively: the loser presents a token the platform has already retired,
its refresh fails, and that account needs a MANUAL re-authorization. There is no
recovery. Three defences, in this order, all on by default:

1. **A lock**, so overlapping jobs on one host serialize. (A host running
   several scheduled publishes a day for one account, plus whatever health
   checks also touch the API, overlaps routinely.)
2. **A re-read INSIDE the lock.** If whoever held the lock first already
   refreshed, adopt THEIR token instead of spending ours. This is the step that
   makes the lock worth having — a lock that only serializes still lets the
   second caller spend a token the first one already replaced.
3. **A cross-machine fallback.** Two hosts cannot share a local lock, so when a
   refresh is REJECTED we re-read the shared store once; if another machine has
   just rotated the token, its copy is sitting there and we adopt it rather than
   failing the publish.

⚠️ THE CROSS-ACCOUNT AUTHORIZATION TRAP
---------------------------------------
The platform issues a token for whichever account the BROWSER was signed into,
not the one named on the command line. Authorizing account B while signed in as
account A silently files A's credentials under B: every later post for B lands
on A, and both labels then share A's pending-share quota, so the upload cap is
reached twice as fast and the cause is invisible. This was found in production.
The save is REFUSED when the returned platform account id already belongs to a
different local label.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from makervox_publish.credentials import CredentialSpec, Credentials
from makervox_publish.errors import IdentityClash, MakervoxPublishError, ReauthorizationRequired
from makervox_publish.logging import get_logger, mask
from makervox_publish.platforms.tiktok.errors import TikTokApiError, is_scope_error

__all__ = ["TikTokAuth", "credential_spec", "SETUP_HINT"]

log = get_logger(__name__)

SETUP_HINT = (
    "Create YOUR OWN TikTok app at the developer portal, add the Content "
    "Posting API product, register a redirect URI (a loopback URL works), then "
    "run `{prog} auth tiktok <account>`."
)


def credential_spec(cfg) -> CredentialSpec:
    """What this platform needs, using the key NAMES from config.

    The names are configurable so several accounts on several developer apps can
    coexist in one environment without colliding on ``TIKTOK_CLIENT_KEY``.
    """
    return CredentialSpec(
        platform="tiktok",
        keys=(cfg.client_key_credential, cfg.client_secret_credential),
        setup=SETUP_HINT.format(prog="makervox_publish"),
        caps=frozenset({"video"}),
    )


class TikTokAuth:
    """Owns the client credentials, the token store and the refresh discipline."""

    def __init__(
        self,
        cfg,
        *,
        credentials: Optional[Credentials] = None,
        token_store: Any = None,
        http: Any = None,
        program_name: str = "makervox_publish",
        account_scopes: Optional[Dict[str, str]] = None,
    ) -> None:
        self.cfg = cfg
        self.creds = credentials or Credentials.default()
        self.store = token_store
        self.http = http
        self.program_name = program_name or "makervox_publish"
        #: per-account scope overrides, from ``accounts.<name>.tiktok.scopes``
        self.account_scopes = dict(account_scopes or {})
        self.spec = credential_spec(cfg)

    # -- configuration ------------------------------------------------------- #
    def client_credentials(self) -> Tuple[Dict[str, str], List[str]]:
        """``(values, missing)``. NEVER raises.

        A platform with missing credentials must be INERT — report why and post
        nothing — rather than aborting a batch that also contains three working
        platforms.
        """
        return self.creds.require(self.spec.keys_required)

    def is_configured(self) -> bool:
        _, missing = self.client_credentials()
        return not missing

    def not_configured_reason(self) -> str:
        _, missing = self.client_credentials()
        return "tiktok not configured: missing {0}. {1}".format(
            ", ".join(missing), SETUP_HINT.format(prog=self.program_name)
        )

    def scopes_for(self, account: str) -> str:
        return self.account_scopes.get(account) or self.cfg.scopes

    def redirect_uri(self) -> str:
        uri = self.cfg.redirect_uri
        if not uri:
            # No default, deliberately: a default redirect URI points at
            # somebody else's domain, which is both an identity leak and a
            # silent misconfiguration that still produces a working-looking
            # OAuth screen.
            raise MakervoxPublishError(
                "platforms.tiktok.redirect_uri is not set. It must match a "
                "redirect URI registered on YOUR OWN TikTok app; "
                "http://127.0.0.1:8722/tiktok/callback works with "
                "`{0} auth tiktok <account>`.".format(self.program_name)
            )
        return uri

    def reauth_hint(self, account: str) -> str:
        return "{0} auth tiktok {1}".format(self.program_name, account)

    # -- step 1: send the user to the platform ------------------------------- #
    def authorize_url(self, account: str, state: Optional[str] = None) -> str:
        """The URL to open in a browser to authorize ``account``.

        ⚠️ Sign OUT of any other account first (or use a private window). The
        token comes back for whoever the browser is signed in as, not for the
        name typed here.
        """
        values, missing = self.client_credentials()
        if missing:
            raise MakervoxPublishError(self.not_configured_reason())
        query = urlencode({
            "client_key": values[self.cfg.client_key_credential],
            "scope": self.scopes_for(account),
            "response_type": "code",
            "redirect_uri": self.redirect_uri(),
            "state": state or account,
        })
        return "{0}?{1}".format(self.cfg.auth_base, query)

    # -- step 2: trade the code for tokens ----------------------------------- #
    def exchange_code(self, account: str, code: str) -> Dict[str, Any]:
        """Exchange an authorization code and store the result under ``account``.

        Refuses the save when the returned platform account already belongs to a
        different local label — see the cross-account trap in the module
        docstring.
        """
        values, missing = self.client_credentials()
        if missing:
            raise MakervoxPublishError(self.not_configured_reason())

        response = self.http.post(
            "{0}/oauth/token/".format(self.cfg.api_base),
            kind="auth",
            data={
                "client_key": values[self.cfg.client_key_credential],
                "client_secret": values[self.cfg.client_secret_credential],
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri(),
            },
        )
        data = self.http.json_body(response)
        if "access_token" not in data:
            raise TikTokApiError(
                "token exchange failed", payload=data, account=account,
                hint="the code may have already been used or expired; codes are "
                     "single-use and short-lived, so start the flow again",
            )

        open_id = data.get("open_id")
        existing = self._identity_clash(account, open_id)
        if existing:
            raise IdentityClash(account, existing, str(open_id))

        record = self._persist(account, data)
        log.info("tiktok: authorized %r (open_id %s, scopes %s)",
                 account, mask(str(open_id or ""), keep=6),
                 record.get("scope") or "(none reported)")
        return record

    def _identity_clash(self, account: str, open_id: Optional[str]) -> Optional[str]:
        if not open_id:
            return None
        stored = self._load_all()
        for name, record in stored.items():
            if name == account or not isinstance(record, dict):
                continue
            if record.get("open_id") == open_id:
                return name
        return None

    # -- step 3: keep it fresh ----------------------------------------------- #
    def access_token(self, account: str) -> str:
        """A valid bearer token for ``account``, refreshing if it has expired."""
        record = self._load_all().get(account)
        if not record:
            raise ReauthorizationRequired("tiktok", account)
        if not self._fresh_enough(record):
            record = self.refresh(account)
        token = record.get("access_token")
        if not token:
            raise ReauthorizationRequired("tiktok", account)
        return token

    def is_authorized(self, account: str) -> bool:
        return bool(self._load_all().get(account))

    def stored_scopes(self, account: str) -> str:
        return (self._load_all().get(account) or {}).get("scope") or ""

    def refresh(self, account: str) -> Dict[str, Any]:
        """Spend the refresh token for a new pair. SINGLE-WRITER SAFE.

        See the module docstring: this is the function the whole locking story
        exists for.
        """
        values, missing = self.client_credentials()
        if missing:
            raise MakervoxPublishError(self.not_configured_reason())

        tokens_cfg = self.cfg.tokens
        if not tokens_cfg.lock_refresh:
            # Named, so it can be understood — not recommended. Without the lock
            # a concurrent refresh permanently spends a refresh token.
            log.warning(
                "tiktok: refreshing %r WITHOUT a lock "
                "(platforms.tiktok.tokens.lock_refresh = false). A concurrent "
                "refresh on this host can permanently spend the refresh token.",
                account,
            )
            self.store.invalidate()
            return self._do_refresh(account, self._load_all(), values, txn=None)

        with self.store.transaction() as txn:
            if not txn.held:
                log.warning(
                    "tiktok: refreshing %r without holding the refresh lock; a "
                    "concurrent refresh can permanently spend the refresh token",
                    account,
                )
            if tokens_cfg.reread_inside_lock:
                current = txn.data.get(account) or {}
                if self._fresh_enough(current):
                    # The winner of the race already refreshed. Adopt their
                    # token instead of spending ours — this is the entire point
                    # of taking the lock.
                    log.debug("tiktok: %r was refreshed while we waited; adopting it",
                              account)
                    return current
            return self._do_refresh(account, txn.data, values, txn=txn)

    def _do_refresh(self, account: str, tokens: Dict[str, Any],
                    values: Dict[str, str], txn: Any) -> Dict[str, Any]:
        current = tokens.get(account) or {}
        refresh_token = current.get("refresh_token")
        if not refresh_token:
            raise ReauthorizationRequired("tiktok", account)

        response = self.http.post(
            "{0}/oauth/token/".format(self.cfg.api_base),
            kind="auth",
            data={
                "client_key": values[self.cfg.client_key_credential],
                "client_secret": values[self.cfg.client_secret_credential],
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        data = self.http.json_body(response)

        if "access_token" not in data:
            adopted = self._adopt_rotated_elsewhere(account, refresh_token)
            if adopted is not None:
                return adopted
            if is_scope_error(data):
                raise ReauthorizationRequired(
                    "tiktok", account, self.scopes_for(account).split(",")
                )
            raise TikTokApiError(
                "token refresh failed for {0!r}".format(account),
                payload=data, account=account,
                hint="the refresh token rotates on use, so a rejected refresh "
                     "usually means it was already spent elsewhere. Re-authorize: "
                     + self.reauth_hint(account),
            )

        record = self._persist(account, data, tokens=tokens, txn=txn)
        return record

    def _adopt_rotated_elsewhere(self, account: str,
                                 spent: str) -> Optional[Dict[str, Any]]:
        """Cross-machine leg: did another host just rotate this token?

        Two machines cannot share a local lock, so a rejection is ambiguous —
        it may mean "already spent by the other host two seconds ago". Re-read
        the shared store ONCE; if a different, still-valid token is sitting
        there, adopt it rather than failing a scheduled publish.
        """
        if not self.cfg.tokens.adopt_remote_on_reject:
            return None
        self.store.invalidate()
        other = (self._load_all().get(account) or {})
        if self._fresh_enough(other) and other.get("refresh_token") != spent:
            log.warning(
                "tiktok: %r was refreshed on another host — adopting that token "
                "instead of failing", account,
            )
            return other
        return None

    # -- storage ------------------------------------------------------------- #
    def _load_all(self) -> Dict[str, Any]:
        data = self.store.load()
        return data if isinstance(data, dict) else {}

    def _fresh_enough(self, record: Optional[Dict[str, Any]]) -> bool:
        return bool(record) and float(record.get("expires_at") or 0) > time.time()

    def _persist(self, account: str, payload: Dict[str, Any],
                 tokens: Optional[Dict[str, Any]] = None,
                 txn: Any = None) -> Dict[str, Any]:
        """Merge a token response into the store, keeping fields it omitted."""
        all_tokens = tokens if tokens is not None else self._load_all()
        keep = dict(all_tokens.get(account) or {})
        expires_in = int(payload.get("expires_in", 86400) or 86400)
        keep.update({
            "open_id": payload.get("open_id", keep.get("open_id")),
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token", keep.get("refresh_token")),
            # A deliberate safety margin against clock skew and in-flight
            # requests: a token that expires mid-upload fails the upload.
            "expires_at": time.time() + expires_in - float(self.cfg.tokens.expiry_skew_s),
            "scope": payload.get("scope", keep.get("scope", "")),
        })
        all_tokens[account] = keep
        if txn is not None:
            txn.save(all_tokens)
        else:
            self.store.save(all_tokens)
        return keep

    # -- diagnostics --------------------------------------------------------- #
    def describe(self) -> str:
        _, missing = self.client_credentials()
        return "tiktok: {0}; tokens in {1}".format(
            "ready" if not missing else "missing " + ", ".join(missing),
            self.store.describe() if self.store is not None else "(no token store)",
        )
