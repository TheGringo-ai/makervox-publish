"""StaticProvider — an explicit in-memory mapping.

For tests, and for library callers that already hold their secrets (a web app
pulling from its own vault) and just want to hand them over.
"""

from __future__ import annotations

from typing import Mapping, Optional

__all__ = ["StaticProvider"]


class StaticProvider:
    def __init__(self, values: Mapping[str, str], name: str = "static") -> None:
        self._values = {k: v for k, v in dict(values).items() if v}
        self.name = name

    def get(self, key: str) -> Optional[str]:
        return self._values.get(key) or None

    def has(self, key: str) -> bool:
        return key in self._values

    def describe(self) -> str:
        return "static mapping ({0} keys)".format(len(self._values))

    def __repr__(self) -> str:  # never leak values through a traceback
        return "<StaticProvider {0} keys>".format(len(self._values))
