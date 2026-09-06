"""EnvProvider — the zero-config default. No dependencies, no network."""

from __future__ import annotations

import os
from typing import Mapping, Optional

__all__ = ["EnvProvider"]


class EnvProvider:
    """Reads secrets from environment variables.

    >>> EnvProvider().get("X_API_KEY")            # doctest: +SKIP
    >>> EnvProvider(prefix="POSTVOX_").get("X_API_KEY")   # reads POSTVOX_X_API_KEY

    ``prefix`` exists so several deployments (or several developer apps) can
    coexist in one shell without renaming every key.
    """

    def __init__(self, prefix: str = "", environ: Optional[Mapping[str, str]] = None) -> None:
        self.prefix = prefix or ""
        self._env = environ if environ is not None else os.environ
        self.name = "env(prefix={0!r})".format(self.prefix) if self.prefix else "env"

    def _name_for(self, key: str) -> str:
        return self.prefix + key

    def get(self, key: str) -> Optional[str]:
        return (self._env.get(self._name_for(key)) or "").strip() or None

    def has(self, key: str) -> bool:
        return bool((self._env.get(self._name_for(key)) or "").strip())

    def describe(self) -> str:
        return "environment variables, prefix={0!r}".format(self.prefix)

    def __repr__(self) -> str:
        return "<EnvProvider prefix={0!r}>".format(self.prefix)
