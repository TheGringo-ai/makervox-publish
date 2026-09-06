"""Chunked FILE_UPLOAD to the TikTok Content Posting API.

THE CHUNK ARITHMETIC IS NOT ``ceil``
------------------------------------
TikTok's init call wants ``video_size``, ``chunk_size`` and
``total_chunk_count`` that satisfy a specific relationship:

* a single chunk when the file fits under the per-chunk ceiling;
* otherwise ``total_chunk_count`` MUST equal ``floor(video_size / chunk_size)``
  and the **last chunk absorbs the remainder**.

Ceil-based math looks obviously right and returns "invalid chunk count" on every
file over the ceiling. The double assignment below (``ceil`` to get a piece
count, divide to get a size, then divide BACK to get the count) is what enforces
the platform's exact relationship, and it is deliberate rather than redundant.

TWO REAL SIZE NUMBERS, AND NEITHER IS A TYPO
--------------------------------------------
* TikTok's DOCUMENTED per-chunk ceiling is 64 MiB.
* 20 MiB is an EMPIRICAL reliability setting — chunks around that size upload
  dependably where larger ones intermittently do not.

Both numbers are real. Do not "correct" one from the other; the config default
is 20 MiB and the docstring records why it is not 64.

CHUNK PUT DETAILS THAT MATTER
-----------------------------
``Content-Range: bytes {start}-{end}/{size}``, an explicit ``Content-Length``
and ``Content-Type: video/mp4`` are all required. **HTTP 206 is SUCCESS**, not a
failure: a partial-content response is what the platform returns for every chunk
but the last, so a naive ``status == 200`` check fails every multi-chunk upload.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Sequence, Tuple

from postvox.errors import PublishError
from postvox.logging import get_logger

__all__ = ["ChunkPlan", "plan_chunks", "upload_chunks", "ACCEPTED_STATUSES"]

log = get_logger(__name__)

#: 206 is a SUCCESSFUL chunk. See the module docstring.
ACCEPTED_STATUSES = (200, 201, 206)


@dataclass(frozen=True)
class ChunkPlan:
    """Exactly what goes into the init call's ``source_info``."""

    video_size: int
    chunk_size: int
    total_chunk_count: int

    def as_source_info(self) -> dict:
        return {
            "source": "FILE_UPLOAD",
            "video_size": self.video_size,
            "chunk_size": self.chunk_size,
            "total_chunk_count": self.total_chunk_count,
        }

    def ranges(self) -> Sequence[Tuple[int, int]]:
        """``(start, length)`` per chunk; the last one absorbs the remainder."""
        out = []
        for index in range(self.total_chunk_count):
            start = index * self.chunk_size
            if index == self.total_chunk_count - 1:
                length = self.video_size - start
            else:
                length = self.chunk_size
            out.append((start, length))
        return out


def plan_chunks(video_size: int, max_chunk_bytes: int) -> ChunkPlan:
    """Split ``video_size`` the way TikTok's init call insists it be split.

    The invariant this function exists to hold:
    ``total_chunk_count == video_size // chunk_size``.
    """
    if video_size <= 0:
        raise PublishError("refusing to upload a zero-byte video")
    if max_chunk_bytes <= 0:
        raise PublishError("platforms.tiktok.upload.max_chunk_bytes must be positive")

    if video_size <= max_chunk_bytes:
        return ChunkPlan(video_size, video_size, 1)

    # ceil -> how many pieces of at most max_chunk_bytes are needed...
    total = -(-video_size // max_chunk_bytes)
    # ...then even them out, and derive the count BACK from the size, so the
    # floor relationship above holds exactly. Do not collapse these two lines.
    chunk_size = video_size // total
    total = video_size // chunk_size
    return ChunkPlan(video_size, chunk_size, total)


def upload_chunks(
    http,
    upload_url: str,
    path: str,
    plan: ChunkPlan,
    *,
    accepted_statuses: Sequence[int] = ACCEPTED_STATUSES,
    timeout_kind: str = "upload_chunk",
) -> None:
    """PUT every chunk in order. Raises :class:`PublishError` on a rejected chunk.

    Chunks are NOT retried here. A chunk PUT that returns a rejection has
    already consumed its slot in the platform's assembly of this upload, and
    re-sending it produces a corrupt or duplicated stream rather than a repair —
    the recovery is a fresh init, which the caller decides about.
    """
    accepted = tuple(accepted_statuses) or ACCEPTED_STATUSES
    size = plan.video_size
    with open(path, "rb") as handle:
        for index, (start, length) in enumerate(plan.ranges()):
            blob = handle.read(length)
            if not blob:
                raise PublishError(
                    "chunk {0}/{1} of {2} read 0 bytes at offset {3}; the file "
                    "changed size during upload".format(
                        index + 1, plan.total_chunk_count, os.path.basename(path), start
                    )
                )
            end = start + len(blob) - 1
            response = http.put(
                upload_url,
                data=blob,
                kind=timeout_kind,
                headers={
                    "Content-Range": "bytes {0}-{1}/{2}".format(start, end, size),
                    "Content-Length": str(len(blob)),
                    "Content-Type": "video/mp4",
                },
            )
            if response.status_code not in accepted:
                raise PublishError(
                    "chunk {0}/{1} rejected: HTTP {2} {3}".format(
                        index + 1, plan.total_chunk_count, response.status_code,
                        (response.text or "")[:200],
                    )
                )
            log.debug("uploaded chunk %d/%d (%d bytes, HTTP %d)",
                      index + 1, plan.total_chunk_count, len(blob),
                      response.status_code)
