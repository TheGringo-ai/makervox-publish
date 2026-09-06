"""The shared Meta token document, and the PageResolver both publishers use.

THE DOCUMENT
------------
One JSON file, one lock, shared by Facebook and Instagram::

    {
      "pages":     {"<page_id>": {"token": "...", "name": "...", "ts": 0}},
      "accounts":  {"<local account name>": "<page_id>"},
      "instagram": {"<local account name>": {"user_id": "...", "token": "...",
                                             "username": "...", "ts": 0}}
    }

``accounts`` was called ``brands`` in the v1 document; it is still read, so an
existing file keeps working, and it is written back under the new name the first
time anything is saved.

WHY PageResolver IS A PROTOCOL
------------------------------
Instagram's PRIMARY publishing path needs the Facebook Page's id and token. In
the original code it reached into the Facebook module for them, which is one
half of a circular import — and the lookup was wrapped in a bare
``except Exception: return (None, None)``, so ANY failure silently downgraded to
the legacy Instagram-login path that the module's own docstring says never
worked ("Insufficient Developer Role").

So this resolver draws a hard line:

* "this account has no linked Page" is a legitimate ``None``;
* "the resolver blew up" RAISES.

An injection mistake must be loud. A silent fallback to a path that cannot work
is indistinguishable, from the outside, from an account that was never set up.
"""

from __future__ import annotations

from typing import Any, Dict, NamedTuple, Optional

from postvox.errors import ConfigError
from postvox.logging import get_logger
from postvox.state.token_store import build_token_store

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - Python < 3.8
    Protocol = object  # type: ignore[assignment]

    def runtime_checkable(cls):  # type: ignore[misc]
        return cls


__all__ = [
    "PageCredentials",
    "PageResolver",
    "MetaTokenStore",
    "StoredPageResolver",
    "EMPTY_DOCUMENT",
]

log = get_logger(__name__)

#: The shape a missing/empty store reports. Keys are always present so callers
#: never have to guard three levels of ``.get()``.
EMPTY_DOCUMENT = {"pages": {}, "accounts": {}, "instagram": {}}

#: v1 name for ``accounts``. Read for compatibility, never written.
_LEGACY_ACCOUNTS_KEY = "brands"


class PageCredentials(NamedTuple):
    """A Facebook Page id and the Page token that acts on it."""

    page_id: str
    token: str


@runtime_checkable
class PageResolver(Protocol):
    """Resolves a local account name to Page credentials.

    Return ``None`` ONLY for "this account has no linked Page". Raise for
    anything else.
    """

    def resolve(self, account: str) -> Optional[PageCredentials]:
        ...


class MetaTokenStore:
    """Typed access to the shared Meta document over any :class:`TokenStore`."""

    def __init__(self, store: Any) -> None:
        self.store = store

    # -- construction -------------------------------------------------------- #
    @classmethod
    def from_config(cls, config: Any, *, store_name: Optional[str] = None) -> "MetaTokenStore":
        """Build from ``[token_stores.<name>]``.

        Facebook and Instagram default to the SAME store name, which is what
        gives them one file and one lock. Pointing them at two stores splits the
        lock and reintroduces the lost-update race, so the default is not
        something to "tidy up".
        """
        name = store_name or config.platforms.instagram.tokens_store
        spec = config.token_store_spec(name)
        return cls(build_token_store(spec, config.state))

    # -- reading ------------------------------------------------------------- #
    def document(self, *, refresh: bool = False) -> Dict[str, Any]:
        if refresh:
            # A cached store would otherwise hand back the snapshot from before
            # the other publisher's write; this is the read half of adopting it.
            self.store.invalidate()
        data = self.store.load()
        if not isinstance(data, dict):
            raise ConfigError(
                "the Meta token store did not return a JSON object; refusing to "
                "guess at its contents",
                key="token_stores",
            )
        return self.normalize(data)

    @staticmethod
    def normalize(data: Dict[str, Any]) -> Dict[str, Any]:
        """Fill in missing top-level keys and fold the v1 ``brands`` map in."""
        out = dict(data)
        for key in EMPTY_DOCUMENT:
            value = out.get(key)
            out[key] = dict(value) if isinstance(value, dict) else {}
        legacy = data.get(_LEGACY_ACCOUNTS_KEY)
        if isinstance(legacy, dict) and legacy:
            merged = dict(legacy)
            merged.update(out["accounts"])   # a v2 entry wins over its v1 twin
            out["accounts"] = merged
        return out

    def page_for(
        self,
        account: str,
        *,
        page_id: Optional[str] = None,
        document: Optional[Dict[str, Any]] = None,
    ) -> Optional[PageCredentials]:
        """Page credentials for ``account``, or None when it has no linked Page.

        ``page_id`` comes from ``accounts.<name>.facebook.page_id`` in config and
        wins over the stored mapping, so an operator can point an account at a
        different Page without editing the token file by hand.
        """
        data = document if document is not None else self.document()
        pages = data["pages"]
        pid = page_id or data["accounts"].get(account)
        if not pid:
            return None
        record = pages.get(str(pid))
        if record is None:
            log.warning(
                "account %r maps to Page %s but no token for that Page is "
                "stored; authorize it before publishing.", account, pid,
            )
            return None
        if not isinstance(record, dict):
            raise ConfigError(
                "the stored record for Page {0} is not an object".format(pid),
                key="token_stores.pages",
            )
        token = record.get("token") or record.get("access_token")
        if not token:
            log.warning("the stored record for Page %s has no token.", pid)
            return None
        return PageCredentials(str(pid), str(token))

    def instagram_for(
        self, account: str, *, document: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """The stored Instagram-login record for ``account`` (legacy path)."""
        data = document if document is not None else self.document()
        record = data["instagram"].get(account)
        if record is None:
            return None
        if not isinstance(record, dict):
            raise ConfigError(
                "the stored Instagram record for {0!r} is not an object".format(account),
                key="token_stores.instagram",
            )
        return record

    def instagram_account_for_user_id(
        self, user_id: str, *, document: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """Which local account name already owns this Instagram user id.

        Used by the cross-account authorization guard: platforms issue a token
        for whichever account the BROWSER was signed into, not the one named on
        the command line.
        """
        data = document if document is not None else self.document()
        for name, record in data["instagram"].items():
            if isinstance(record, dict) and str(record.get("user_id") or "") == str(user_id):
                return name
        return None

    # -- writing ------------------------------------------------------------- #
    def transaction(self):
        """The underlying store's locked, re-read-inside-the-lock transaction."""
        return self.store.transaction()

    def save_instagram(self, account: str, record: Dict[str, Any]) -> None:
        """Merge one Instagram record in, under the store's lock.

        Read-modify-write on a file two publishers share: without the
        transaction, a Page token written by the Facebook side between this
        store's read and its write would be silently dropped.
        """
        with self.transaction() as txn:
            data = self.normalize(txn.data)
            data["instagram"][account] = record
            data.pop(_LEGACY_ACCOUNTS_KEY, None)
            txn.save(data)

    def describe(self) -> str:
        describer = getattr(self.store, "describe", None)
        return describer() if callable(describer) else repr(self.store)


class StoredPageResolver:
    """The default :class:`PageResolver`: read the shared Meta token document.

    ``page_ids`` maps a local account name to a Page id from config
    (``accounts.<name>.facebook.page_id``), so a Page can be named in the config
    file rather than only discovered through an authorization flow.
    """

    def __init__(
        self,
        tokens: MetaTokenStore,
        page_ids: Optional[Dict[str, str]] = None,
    ) -> None:
        self.tokens = tokens
        self.page_ids = dict(page_ids or {})

    @classmethod
    def from_config(cls, config: Any, tokens: Optional[MetaTokenStore] = None
                    ) -> "StoredPageResolver":
        store = tokens or MetaTokenStore.from_config(config)
        page_ids = {
            name: account.facebook_page_id
            for name, account in config.accounts.items()
            if account.facebook_page_id
        }
        return cls(store, page_ids)

    def resolve(self, account: str) -> Optional[PageCredentials]:
        """None means "no linked Page". Anything else raises — deliberately."""
        return self.tokens.page_for(account, page_id=self.page_ids.get(account))

    def describe(self) -> str:
        return "stored Page tokens from {0}".format(self.tokens.describe())
