"""Media staging: Instagram PULLS by URL, so local bytes must be served first.

There is no way to upload a file to the Instagram publishing API. You hand it a
URL, it fetches the media itself, and the URL has to be reachable from Meta's
network for the duration of the publish. So a local mp4 becomes: stage it
somewhere publicly fetchable, publish, delete it.

That "delete it" is a SECURITY step, not tidy-up. A staged reel that fails to
delete stays publicly readable at a guessable URL forever, so a failed cleanup is
logged at ERROR with the full location — never swallowed and never a debug line.

A stager is a plugin (``platforms.instagram.staging.impl``), because where you
can serve a file from is the one thing this package cannot guess. There is NO
default: bucket names, cloud projects and web roots belong to you, and
defaulting them to somebody else's is both an identity leak and a
silent-misconfiguration trap.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional

from postvox.logging import get_logger

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - Python < 3.8
    Protocol = object  # type: ignore[assignment]

    def runtime_checkable(cls):  # type: ignore[misc]
        return cls


__all__ = ["StagedMedia", "MediaStager", "staged", "content_type_for"]

log = get_logger(__name__)

_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def content_type_for(path: str, default: str = "application/octet-stream") -> str:
    """Content type from the extension, with the query string stripped first.

    Signed and public URLs carry query parameters that would otherwise defeat
    extension sniffing.
    """
    clean = str(path).split("?")[0].lower()
    return _CONTENT_TYPES.get(os.path.splitext(clean)[1], default)


@dataclass(frozen=True)
class StagedMedia:
    """A publicly fetchable URL plus what it takes to remove it again."""

    url: str
    key: str = ""
    #: Human-readable location for log messages, e.g. ``gs://bucket/object``.
    #: Present in the cleanup-failure message so an orphan can actually be found.
    location: str = ""
    stager: Optional[Any] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def release(self) -> None:
        """Remove the staged object. Raises; :func:`staged` does the logging."""
        if self.stager is not None:
            self.stager.unstage(self)

    def __str__(self) -> str:
        return self.location or self.url


@runtime_checkable
class MediaStager(Protocol):
    """Put a local file somewhere the platform can fetch it, then take it away."""

    def stage(self, local_path: str, *, content_type: Optional[str] = None) -> StagedMedia:
        ...

    def unstage(self, staged: StagedMedia) -> None:
        ...

    def describe(self) -> str:
        """One line for ``postvox doctor``. Never a credential."""
        ...


@contextmanager
def staged(
    stager: MediaStager,
    local_path: str,
    *,
    content_type: Optional[str] = None,
    delete_after: bool = True,
) -> Iterator[StagedMedia]:
    """Stage ``local_path``, yield it, and always attempt cleanup.

    Cleanup runs in a ``finally`` so it happens on the exception path too — that
    is the path where an orphan is most likely, because a publish that failed
    part-way still left the object public.
    """
    media = stager.stage(local_path, content_type=content_type)
    try:
        yield media
    finally:
        if delete_after:
            try:
                media.release()
            except Exception as exc:  # noqa: BLE001 - never mask the publish result
                log.error(
                    "staged media cleanup FAILED — %s is still PUBLICLY READABLE "
                    "and must be deleted by hand: %s: %s",
                    media, type(exc).__name__, exc,
                )
