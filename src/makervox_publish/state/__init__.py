"""makervox_publish.state — everything this package writes, and the locking around it.

One root directory (``[state] dir``), created on FIRST WRITE only, holding token
stores, the delivery ledger and the per-platform counters. Every mutable file
here is a read-modify-write, so each one is reached through an advisory lock and
written temp-then-rename; see :mod:`makervox_publish.state.locks` for why running
unlocked has to be typed out rather than defaulted to.
"""

from __future__ import annotations

from makervox_publish.state.atomic import (
    atomic_write_bytes,
    atomic_write_text,
    read_json,
    write_json,
)
from makervox_publish.state.locks import (
    FileLock,
    LockPolicy,
    file_lock,
    lock_path_for,
    locked,
    resolve_backend,
)
from makervox_publish.state.paths import ensure_dir, ensure_parent, expand, under
from makervox_publish.state.token_store import (
    FileTokenStore,
    MirroredTokenStore,
    SecretManagerTokenStore,
    TokenStore,
    TokenTransaction,
    build_token_store,
)

__all__ = [
    # paths
    "expand",
    "ensure_dir",
    "ensure_parent",
    "under",
    # atomic writes
    "atomic_write_bytes",
    "atomic_write_text",
    "write_json",
    "read_json",
    # locking
    "LockPolicy",
    "locked",
    "FileLock",
    "file_lock",
    "lock_path_for",
    "resolve_backend",
    # token storage
    "TokenStore",
    "TokenTransaction",
    "FileTokenStore",
    "SecretManagerTokenStore",
    "MirroredTokenStore",
    "build_token_store",
]
