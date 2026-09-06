"""write-temp + ``os.replace`` + chmod, and the "dirs on first WRITE only" rule.

Two properties this module exists to guarantee:

* **Nothing is created as a side effect of reading.** Importing makervox_publish, loading
  a config or resolving credentials must not create a directory in ``$HOME``.
  Directories appear the first time something is actually written.
* **A token file is never briefly world-readable.** The temp file is chmod'ed
  BEFORE the rename, not after, so there is no window in which the finished
  content sits at the umask default.

The rename itself is atomic only within one filesystem, so the temp file is
always created in the destination's own directory.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Optional

from makervox_publish.logging import get_logger
from makervox_publish.state.paths import ensure_dir as _ensure_dir

__all__ = [
    "ensure_dir",
    "atomic_write_bytes",
    "atomic_write_text",
    "write_json",
    "read_json",
]

log = get_logger(__name__)

#: One warning per process, not one per write: a host with no POSIX modes
#: (SMB/FAT/Windows) would otherwise emit a line for every token refresh.
_mode_warned = False


def ensure_dir(path: str, mode: int = 0o700) -> None:
    """Create ``path`` and its parents if missing. WRITE path only.

    Thin alias for :func:`makervox_publish.state.paths.ensure_dir` so there is one
    implementation of the create-on-first-write rule and ONE process-wide
    "this filesystem cannot do 0700" warning, not two that each fire once.
    """
    if not path:
        return
    _ensure_dir(path, mode)


def _apply_file_mode(fd: int, path: str, file_mode: int, enforce: bool) -> None:
    global _mode_warned
    if not enforce:
        if not _mode_warned:
            _mode_warned = True
            log.warning(
                "state.enforce_file_mode is off: %s and other credential files "
                "are being written WITHOUT owner-only permissions. Anything "
                "that can read your filesystem can read your tokens.",
                os.path.basename(path),
            )
        return
    try:
        os.fchmod(fd, file_mode)
    except (OSError, AttributeError) as exc:  # no fchmod on some platforms
        if not _mode_warned:
            _mode_warned = True
            log.warning(
                "could not set owner-only permissions on %s (%s); credentials "
                "are stored without them on this filesystem. Set "
                "state.enforce_file_mode = false to silence this.",
                path, exc,
            )


def atomic_write_bytes(
    path: str,
    data: bytes,
    *,
    file_mode: int = 0o600,
    dir_mode: int = 0o700,
    enforce_file_mode: bool = True,
) -> None:
    """Replace ``path`` with ``data`` atomically.

    A truncate-in-place write loses the old content when the process dies
    mid-write, and for a token file that is unrecoverable: the refresh token it
    held may already have been spent.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    ensure_dir(directory, dir_mode)

    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                               dir=directory)
    try:
        _apply_file_mode(fd, path, file_mode, enforce_file_mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = ""
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:  # pragma: no cover - best effort
                pass


def atomic_write_text(path: str, text: str, **kwargs: Any) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), **kwargs)


def write_json(path: str, obj: Any, **kwargs: Any) -> None:
    """Serialize ``obj`` and write it atomically."""
    atomic_write_text(path, json.dumps(obj, indent=1, sort_keys=True) + "\n", **kwargs)


def read_json(path: str, default: Optional[Any] = None) -> Any:
    """Read JSON, returning ``default`` for a missing or unparseable file.

    A corrupt file is reported at WARNING rather than swallowed: silently
    handing back an empty store reads downstream as "this account was never
    connected", which sends an operator to re-authorize when the real problem is
    a truncated file they could still recover.
    """
    try:
        with open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.warning(
            "could not read %s (%s); treating it as empty. If this file held "
            "tokens, do NOT re-authorize before checking whether it is "
            "recoverable.", path, exc,
        )
        return default
