"""postvox.credentials — pluggable secret resolution.

Zero-config usage. This is the whole story for most self-hosters::

    from postvox.credentials import Credentials

    creds = Credentials.default()              # env vars only
    values, missing = creds.require(["X_API_KEY", "X_API_SECRET"])
    if missing:
        return False, "not configured: " + ", ".join(missing)

The two-tuple ``(values, missing)`` contract is load-bearing: a platform whose
credentials are absent must be INERT — report why and post nothing — rather
than raising and aborting a batch that also contains three working platforms.
"""

from __future__ import annotations

import time
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from postvox.credentials.base import (
    ChainProvider,
    CredentialError,
    CredentialNotFound,
    CredentialProvider,
    ProviderUnavailable,
)
from postvox.credentials.env import EnvProvider
from postvox.credentials.static import StaticProvider
from postvox.logging import get_logger, register_secret

__all__ = [
    "Credentials",
    "CredentialSpec",
    "CredentialProvider",
    "ChainProvider",
    "EnvProvider",
    "StaticProvider",
    "CredentialError",
    "CredentialNotFound",
    "ProviderUnavailable",
]

log = get_logger(__name__)


class Credentials:
    """A provider chain plus a TTL cache and the ``(values, missing)`` contract."""

    def __init__(
        self,
        providers: Optional[Sequence[object]] = None,
        *,
        cache_ttl_s: float = 300.0,
        redact_in_logs: bool = True,
    ) -> None:
        self._chain = ChainProvider(list(providers) if providers else [EnvProvider()])
        self._ttl = float(cache_ttl_s)
        self._redact = bool(redact_in_logs)
        self._cache = {}  # type: Dict[str, Tuple[float, Optional[str]]]

    # -- construction ------------------------------------------------------- #
    @classmethod
    def default(cls) -> "Credentials":
        """Environment variables only. No config file needed, ever."""
        return cls([EnvProvider()])

    @classmethod
    def from_config(cls, cfg: Optional[object]) -> "Credentials":
        """Build from the ``[credentials]`` block of the config file.

        Each provider entry is ``{"impl": "module.path:ClassName",
        "options": {...}}``. ``impl`` is resolved by import and, failing that,
        against the ``postvox.credential_providers`` entry-point group, so third
        parties can publish providers (Vault, AWS SM, 1Password) WITHOUT this
        package depending on any of them.

        An absent or empty block yields :meth:`default` — env vars only.
        """
        from postvox.config.plugins import instantiate  # layered below us

        if cfg is None:
            return cls.default()

        if isinstance(cfg, Mapping):
            entries = list(cfg.get("providers") or [])
            ttl = float(cfg.get("cache_ttl_s", 300.0))
            redact = bool(cfg.get("redact_in_logs", True))
        else:  # a CredentialsConfig dataclass
            entries = list(getattr(cfg, "providers", ()) or [])
            ttl = float(getattr(cfg, "cache_ttl_s", 300.0))
            redact = bool(getattr(cfg, "redact_in_logs", True))

        if not entries:
            return cls(cache_ttl_s=ttl, redact_in_logs=redact)

        providers = []  # type: List[object]
        for index, entry in enumerate(entries):
            key = "credentials.providers[{0}]".format(index)
            if isinstance(entry, Mapping):
                impl = entry.get("impl")
                options = dict(entry.get("options") or {})
            else:
                impl = getattr(entry, "impl", None)
                options = dict(getattr(entry, "options", {}) or {})
            required = bool(options.pop("required_provider", True))
            try:
                providers.append(
                    instantiate(impl, options, key=key,
                                group="postvox.credential_providers")
                )
            except ProviderUnavailable as exc:
                if required:
                    raise
                log.warning("credential provider %s unavailable, skipping: %s", impl, exc)
        if not providers:
            log.warning("no usable credential providers configured; falling back to env")
            providers.append(EnvProvider())
        return cls(providers, cache_ttl_s=ttl, redact_in_logs=redact)

    # -- resolution --------------------------------------------------------- #
    def get(self, key: str, *, default: Optional[str] = None) -> Optional[str]:
        """Resolve one key, cached for ``cache_ttl_s``. Returns ``default`` on a miss."""
        now = time.time()
        hit = self._cache.get(key)
        if hit is not None and (self._ttl <= 0 or now - hit[0] < self._ttl):
            return hit[1] if hit[1] is not None else default

        value = self._chain.get(key)
        self._cache[key] = (now, value)
        if value and self._redact:
            # So an accidental interpolation into a log record is scrubbed.
            register_secret(value)
        return value if value is not None else default

    def require_one(self, key: str) -> str:
        """Resolve one key or raise, naming every provider that was tried."""
        value = self.get(key)
        if not value:
            raise CredentialNotFound(
                key, [getattr(p, "name", type(p).__name__) for p in self._chain.providers]
            )
        return value

    def require(self, keys: Iterable[str]) -> Tuple[Dict[str, str], List[str]]:
        """Resolve many. Returns ``(values, missing)`` and NEVER raises."""
        values = {}  # type: Dict[str, str]
        missing = []  # type: List[str]
        for key in keys:
            value = self.get(key)
            if value:
                values[key] = value
            else:
                missing.append(key)
        return values, missing

    def invalidate(self, key: Optional[str] = None) -> None:
        """Drop the cache for one key, or all of it (after a rotation)."""
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)

    # -- diagnostics -------------------------------------------------------- #
    def status(self, spec: Mapping[str, Sequence[str]]) -> Dict[str, dict]:
        """``{platform: {"ready", "missing", "keys"}}`` for ``postvox doctor``.

        Reports NAMES and presence only — never values.
        """
        out = {}  # type: Dict[str, dict]
        for platform, keys in spec.items():
            if hasattr(keys, "keys_required"):  # a CredentialSpec
                keys = keys.keys_required  # type: ignore[assignment]
            _, missing = self.require(list(keys))
            out[platform] = {
                "ready": not missing,
                "missing": missing,
                "keys": list(keys),
            }
        return out

    def describe(self) -> str:
        return self._chain.describe()

    def __repr__(self) -> str:  # never leak values through a traceback
        return "<Credentials {0}>".format(self._chain.name)


class CredentialSpec:
    """What a platform declares that it needs.

    Replaces module-level NEEDS / SETUP / CAPS constants with a real object, so
    the registry does not duck-type on module attributes::

        TIKTOK = CredentialSpec(
            platform="tiktok",
            keys=("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"),
            setup="Create YOUR OWN TikTok app (Content Posting API), then run "
                  "`postvox auth tiktok <account>`.",
            caps=frozenset({"video"}),
        )

    Key names are DEFAULTS. Every one is overridable per platform in config
    (``client_key_credential = "MY_OWN_NAME"``) so several accounts on several
    developer apps can coexist in one environment.
    """

    __slots__ = ("platform", "keys_required", "keys_optional", "setup", "caps")

    def __init__(
        self,
        platform: str,
        keys: Sequence[str],
        setup: str = "",
        caps: Optional[frozenset] = None,
        optional_keys: Sequence[str] = (),
    ) -> None:
        self.platform = platform
        self.keys_required = tuple(keys)
        self.keys_optional = tuple(optional_keys)
        self.setup = setup
        self.caps = caps or frozenset()

    def resolve(self, creds: Credentials) -> Tuple[Dict[str, str], List[str]]:
        """``(values, missing)`` for the required keys; optional ones are merged in."""
        values, missing = creds.require(self.keys_required)
        for key in self.keys_optional:
            value = creds.get(key)
            if value:
                values[key] = value
        return values, missing

    def ready(self, creds: Credentials) -> bool:
        _, missing = creds.require(self.keys_required)
        return not missing

    def not_configured_reason(self, missing: Sequence[str]) -> str:
        return "{0} not configured: missing {1}. {2}".format(
            self.platform, ", ".join(missing), self.setup
        ).strip()

    def __repr__(self) -> str:
        return "<CredentialSpec {0} keys={1}>".format(self.platform, list(self.keys_required))
