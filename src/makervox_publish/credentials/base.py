"""The credential-provider interface and the chain that walks it.

DESIGN CONSTRAINTS
  * Environment variables are the ZERO-CONFIG default. ``Credentials.default()``
    works with no config file, no cloud SDK, no keyring and no network.
  * Importing this module must never import google-cloud-*, keyring or boto3,
    and must never shell out to anything.
  * Config files reference secret NAMES, never values.
  * Providers resolve names to strings. They never log values, never write, and
    never raise for a merely-absent key (that is ``None``).
"""

from __future__ import annotations

from typing import Optional, Sequence

try:  # pragma: no cover - typing only
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - Python < 3.8
    Protocol = object  # type: ignore[assignment]

    def runtime_checkable(cls):  # type: ignore[misc]
        return cls

from makervox_publish.errors import MakervoxPublishError
from makervox_publish.logging import get_logger

__all__ = [
    "CredentialError",
    "CredentialNotFound",
    "ProviderUnavailable",
    "CredentialProvider",
    "ChainProvider",
]

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class CredentialError(MakervoxPublishError):
    """Base class for credential-resolution problems."""


class CredentialNotFound(CredentialError):
    """A required credential could not be resolved by any provider."""

    def __init__(self, key: str, tried: Sequence[str]) -> None:
        super().__init__(
            "credential {0!r} not found. Tried: {1}. Set the {0} environment "
            "variable, or add a provider in makervox-publish.toml.".format(
                key, ", ".join(tried) or "(no providers)"
            )
        )
        self.key = key
        self.tried = list(tried)


class ProviderUnavailable(CredentialError):
    """A provider could not start (missing extra, bad config, no auth).

    Raised at CONSTRUCTION time, not per-lookup, so a broken provider is a
    startup error rather than an intermittent one. A provider declared with
    ``required = false`` is skipped with a WARNING instead.
    """


# --------------------------------------------------------------------------- #
# the interface — implement these three methods and you are a provider
# --------------------------------------------------------------------------- #
@runtime_checkable
class CredentialProvider(Protocol):
    """Resolves secret NAMES to secret VALUES.

    Implementations MUST be read-only, side-effect free and safe to call from
    multiple threads. They MUST NOT log or repr the returned value.
    """

    name: str

    def get(self, key: str) -> Optional[str]:
        """Return the secret for ``key``, or None if this provider lacks it.

        None means "not mine" and lets the chain continue. Raise
        :class:`ProviderUnavailable` ONLY for a broken provider (revoked auth,
        unreachable backend) so the chain reports it distinctly from a miss.
        An empty string is treated as a miss.
        """
        ...

    def has(self, key: str) -> bool:
        """Cheap existence probe.

        A remote provider must implement this from ONE bulk listing rather than
        a per-key round trip: a subprocess per missing key costs about a second
        each and turns "this platform is not configured" into a visible stall.
        """
        ...

    def describe(self) -> str:
        """One line for ``makervox_publish doctor``. Must never include a secret value."""
        ...


# --------------------------------------------------------------------------- #
# the chain
# --------------------------------------------------------------------------- #
class ChainProvider:
    """First non-empty answer wins. Order is the order given."""

    def __init__(self, providers: Sequence[object]) -> None:
        self.providers = list(providers)
        self.name = "chain(" + ", ".join(getattr(p, "name", type(p).__name__)
                                         for p in self.providers) + ")"
        self._broken = set()  # type: set

    def get(self, key: str) -> Optional[str]:
        for provider in self.providers:
            pname = getattr(provider, "name", type(provider).__name__)
            try:
                if not provider.has(key):  # type: ignore[attr-defined]
                    continue
                value = provider.get(key)  # type: ignore[attr-defined]
            except ProviderUnavailable as exc:
                # A broken provider must not mask a working one further down.
                if pname not in self._broken:
                    self._broken.add(pname)
                    log.warning("credential provider %s unavailable, skipping: %s",
                                pname, exc)
                continue
            except Exception as exc:  # a third-party provider misbehaving
                if pname not in self._broken:
                    self._broken.add(pname)
                    log.warning("credential provider %s raised %s, skipping: %s",
                                pname, type(exc).__name__, exc)
                continue
            if value:
                return value
        return None

    def has(self, key: str) -> bool:
        for provider in self.providers:
            try:
                if provider.has(key):  # type: ignore[attr-defined]
                    return True
            except Exception:
                continue
        return False

    def describe(self) -> str:
        return " -> ".join(
            p.describe() if hasattr(p, "describe") else type(p).__name__  # type: ignore[attr-defined]
            for p in self.providers
        )

    def __repr__(self) -> str:
        return "<ChainProvider {0}>".format(self.name)
