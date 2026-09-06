"""Where postvox writes, and WHEN those directories come into existence.

One rule, and it is the whole module: **directories are created on first WRITE
only, never at import time and never by merely loading a config.** The code this
package was extracted from created a tree under the user's ``$HOME`` as a side
effect of ordinary calls, so simply importing it and asking a question left
files behind on the machine.

The second rule is that anything holding a credential is owner-only. On
filesystems with no POSIX modes (SMB, FAT, some Windows shares) ``chmod`` is a
no-op or raises; that is reported ONCE at WARNING rather than silently degrading
to world-readable tokens.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from postvox.logging import get_logger

__all__ = [
    "expand",
    "ensure_dir",
    "ensure_parent",
    "chmod_quietly",
    "reset_mode_warning",
    "under",
    "exists",
]

log = get_logger(__name__)

_lock = threading.Lock()
#: One warning per process, not one per write. A publisher that writes a token
#: every few minutes would otherwise bury its own logs.
_mode_warning_emitted = False


def expand(path: str) -> str:
    """``~`` and ``$VAR`` expansion, then absolute. No filesystem access."""
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))


def ensure_dir(path: str, mode: int = 0o700) -> str:
    """Create ``path`` (and parents) if needed. Call this from a WRITE path only."""
    target = expand(path)
    if not os.path.isdir(target):
        os.makedirs(target, exist_ok=True)
        # makedirs applies `mode` only to the leaf it creates and umask eats
        # part of it, so set it explicitly on the leaf we care about.
        chmod_quietly(target, mode)
    return target


def ensure_parent(path: str, mode: int = 0o700) -> str:
    """Create the parent directory of a file we are about to write."""
    target = expand(path)
    parent = os.path.dirname(target)
    if parent:
        ensure_dir(parent, mode)
    return target


def chmod_quietly(path: str, mode: int, *, enforce: bool = True) -> bool:
    """``chmod``, returning whether the mode actually took.

    Returns False (with ONE process-wide warning) when the filesystem cannot
    represent POSIX modes. The caller keeps going — refusing to publish because
    a share cannot do 0600 would be worse — but the operator is told that
    credentials on this host are not owner-only.
    """
    global _mode_warning_emitted
    if not enforce:
        return False
    try:
        os.chmod(path, mode)
        return True
    except (OSError, NotImplementedError) as exc:
        with _lock:
            first = not _mode_warning_emitted
            _mode_warning_emitted = True
        if first:
            log.warning(
                "cannot set owner-only permissions (%o) on %s: %s. Credentials "
                "and state on this filesystem are stored WITHOUT owner-only "
                "permissions; set state.enforce_file_mode false to silence this.",
                mode, path, exc,
            )
        return False


def reset_mode_warning() -> None:
    """Test hook: allow the one-shot permissions warning to fire again."""
    global _mode_warning_emitted
    with _lock:
        _mode_warning_emitted = False


def under(state_dir: str, *parts: str) -> str:
    """Join a path under the state root without creating anything."""
    return expand(os.path.join(state_dir, *parts))


def exists(path: Optional[str]) -> bool:
    return bool(path) and os.path.exists(expand(str(path)))
