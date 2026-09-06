"""content_sha() — the short content fingerprint that IS a post's identity.

WHY A HASH AND NOT A FILENAME
-----------------------------
A scheduler that renders one content type per slot and names each file
``<type>_<date>.mp4`` has no slot discriminator in the name, so when the same
type comes up twice in one day the SECOND render silently overwrites the first
and two genuinely different videos share one filename. That is not
hypothetical: on a rotation of 52 slots advancing one per run at four runs a
day, with one type landing on every even slot, EVERY day renders that type
twice. It happened — two completely different readings, hours apart, same
filename. Deduping on the filename would have suppressed a real post.

TWO FROZEN DECISIONS
--------------------
* ``hex_chars = 16``. Changing it orphans every existing ledger row, so it is
  frozen in config with a comment saying exactly that.
* Hash the SOURCE media, never the derived file that goes over the wire. One
  code path may upload a 15-second cut while a recovery path uploads the full
  clip; hashing the wire bytes would never match across the two, and the
  recovery path would re-post everything forever.

Unreadable file -> "". The caller then fails OPEN (publishes), because "cannot
prove this is the same post" must never become "suppress it".
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

from postvox.logging import get_logger

__all__ = ["content_sha", "DEFAULT_HEX_CHARS"]

log = get_logger(__name__)

#: FROZEN. See the module docstring.
DEFAULT_HEX_CHARS = 16

#: 1 MiB reads. Large enough that hashing a 60 MiB reel is a handful of syscalls,
#: small enough that it never doubles the process's resident size.
_CHUNK = 1 << 20


def content_sha(
    path: str,
    *,
    algorithm: str = "sha256",
    hex_chars: int = DEFAULT_HEX_CHARS,
    quiet: bool = False,
) -> str:
    """Short content fingerprint of ``path``, or ``""`` when it cannot be read.

    Never raises. A missing or unreadable file is a legitimate outcome here —
    a recovery path may be asked about a file that has since been archived —
    and it means "identity unknown", not "failure".
    """
    full = os.path.abspath(os.path.expanduser(str(path)))
    try:
        digest = hashlib.new(algorithm)
    except ValueError:
        log.warning("unknown fingerprint algorithm %r; falling back to sha256", algorithm)
        digest = hashlib.sha256()

    try:
        with open(full, "rb") as handle:
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        if not quiet:
            # Not an error: it downgrades dedupe to fail-open for this post, and
            # saying so is the difference between "a duplicate slipped through"
            # and "why did dedupe stop working".
            log.warning(
                "cannot fingerprint %s (%s); duplicate detection will fail OPEN "
                "for this post", full, exc,
            )
        return ""

    return digest.hexdigest()[: max(1, int(hex_chars))]


def sha_for(path: Optional[str], fingerprint_cfg=None, **kwargs) -> str:
    """``content_sha`` driven by a :class:`postvox.config.FingerprintConfig`."""
    if not path:
        return ""
    if fingerprint_cfg is None:
        return content_sha(path, **kwargs)
    return content_sha(
        path,
        algorithm=str(getattr(fingerprint_cfg, "algorithm", "sha256")),
        hex_chars=int(getattr(fingerprint_cfg, "hex_chars", DEFAULT_HEX_CHARS)),
        **kwargs
    )
