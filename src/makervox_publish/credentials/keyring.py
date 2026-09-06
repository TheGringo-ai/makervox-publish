"""KeyringProvider — the OS keychain. Optional extra: pip install makervox-publish[keyring].

Raises ProviderUnavailable at CONSTRUCTION when the extra is absent, so a
missing dependency is a startup error rather than an intermittent miss.
"""

from __future__ import annotations

from typing import Optional

from makervox_publish.credentials.base import ProviderUnavailable

__all__ = ["KeyringProvider"]


class KeyringProvider:
    def __init__(self, service: str = "makervox_publish") -> None:
        try:
            import keyring as _keyring  # noqa: WPS433 - optional extra
        except ImportError as exc:
            raise ProviderUnavailable(
                "the keyring extra is not installed: pip install 'makervox-publish[keyring]'"
            ) from exc
        try:
            backend = _keyring.get_keyring()
        except Exception as exc:  # a headless box with no usable backend
            raise ProviderUnavailable("no usable keyring backend: {0}".format(exc)) from exc

        self._keyring = _keyring
        self.service = service
        self.name = "keyring({0})".format(service)
        self._backend_name = type(backend).__name__

    def get(self, key: str) -> Optional[str]:
        try:
            value = self._keyring.get_password(self.service, key)
        except Exception as exc:
            raise ProviderUnavailable(
                "keyring lookup failed (locked keychain?): {0}".format(exc)
            ) from exc
        return (value or "").strip() or None

    def has(self, key: str) -> bool:
        # The keychain has no cheap listing API; a lookup IS the probe, and it
        # is local, so this costs nothing worth optimizing.
        return self.get(key) is not None

    def describe(self) -> str:
        return "OS keyring service={0!r} backend={1}".format(self.service, self._backend_name)

    def __repr__(self) -> str:
        return "<KeyringProvider {0}>".format(self.service)
