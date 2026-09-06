"""Portable advisory locking: fcntl / msvcrt / an explicitly-chosen none.

WHY THIS IS NOT JUST ``import fcntl``
-------------------------------------
A module-level ``import fcntl`` makes ``import makervox_publish`` fail outright on
Windows. Backend selection happens here, once, so no other module has to care.

WHY ``none`` MUST BE TYPED OUT
------------------------------
Locking is what stops two processes from refreshing the same rotating refresh
token at once. A platform that rotates the refresh token on use SPENDS the old
one on a successful refresh, so the loser of that race holds a dead token and
needs a manual re-authorization — an unrecoverable loss, not a retryable error.
The package therefore refuses to run unlocked by accident: if no backend is
available, that is a ConfigError telling the operator to set
``state.locks.backend = "none"`` deliberately.

``on_unavailable`` stays ``proceed`` by default (never drop a post over a lock
file) but always logs, because on a multi-machine deployment "proceed" is the
wrong call and the operator needs to see that it happened.
"""

from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from makervox_publish.errors import ConfigError, LockUnavailable
from makervox_publish.logging import get_logger
from makervox_publish.state.paths import ensure_dir, under

__all__ = [
    "resolve_backend",
    "FileLock",
    "file_lock",
    "lock_path_for",
    "LockPolicy",
    "locked",
]

log = get_logger(__name__)

_BACKENDS = ("auto", "fcntl", "msvcrt", "none")

#: Only warn once per process about running unlocked, or a busy publisher fills
#: the log with the same line.
_none_warned = False


def resolve_backend(backend: str = "auto") -> str:
    """Turn ``auto`` into the concrete backend for this host."""
    if backend not in _BACKENDS:
        raise ConfigError(
            "unknown lock backend {0!r}; use one of {1}".format(backend, list(_BACKENDS)),
            key="state.locks.backend",
        )
    if backend != "auto":
        return backend
    if os.name == "posix":
        return "fcntl"
    if os.name == "nt":
        return "msvcrt"
    raise ConfigError(
        "no advisory-lock backend is available on this platform (os.name="
        "{0!r}). Running unlocked can destroy a rotating refresh token, so it "
        'must be chosen deliberately: set state.locks.backend = "none".'.format(os.name),
        key="state.locks.backend",
    )


def lock_path_for(name: str, directory: str) -> str:
    """Path of the lock file for a named lock."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(name))
    return os.path.join(directory, safe + ".lock")


class FileLock:
    """A blocking-with-timeout advisory lock on one file.

    Reentrant within one instance (nested ``with`` on the same object is a
    no-op), which matters because a token transaction may be nested inside a
    publish lock held by the same call stack.
    """

    def __init__(
        self,
        path: str,
        *,
        backend: str = "auto",
        timeout_s: float = 120.0,
        on_unavailable: str = "proceed",
        poll_interval_s: float = 0.1,
        dir_mode: int = 0o700,
    ) -> None:
        self.path = path
        self.backend = resolve_backend(backend)
        self.timeout_s = float(timeout_s)
        self.on_unavailable = on_unavailable
        self.poll_interval_s = float(poll_interval_s)
        self.dir_mode = dir_mode
        self._fd = None  # type: Optional[int]
        self._depth = 0
        self.held = False

    # -- acquisition --------------------------------------------------------- #
    def _try_lock(self, fd: int) -> bool:
        if self.backend == "fcntl":
            import fcntl  # local: absent on Windows

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    return False
                raise
        if self.backend == "msvcrt":
            import msvcrt  # local: absent on POSIX

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        return True  # backend == "none"

    def acquire(self) -> bool:
        """Take the lock. Returns True when it is actually held."""
        global _none_warned

        if self._depth:
            self._depth += 1
            return self.held

        if self.backend == "none":
            if not _none_warned:
                _none_warned = True
                log.warning(
                    "advisory locking is DISABLED (state.locks.backend = none). "
                    "Concurrent token refreshes can destroy a rotating refresh "
                    "token, which needs a manual re-authorization to recover."
                )
            self._depth = 1
            self.held = False
            return False

        ensure_dir(os.path.dirname(os.path.abspath(self.path)) or ".", self.dir_mode)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            return self._unavailable("cannot open lock file {0}: {1}".format(self.path, exc))

        deadline = time.time() + self.timeout_s
        while True:
            try:
                if self._try_lock(fd):
                    self._fd = fd
                    self._depth = 1
                    self.held = True
                    return True
            except OSError as exc:
                os.close(fd)
                return self._unavailable("lock {0} failed: {1}".format(self.path, exc))
            if time.time() >= deadline:
                os.close(fd)
                return self._unavailable(
                    "lock {0} still held after {1:.0f}s".format(self.path, self.timeout_s)
                )
            time.sleep(self.poll_interval_s)

    def _unavailable(self, detail: str) -> bool:
        if self.on_unavailable == "fail":
            raise LockUnavailable(detail)
        log.warning("%s; proceeding WITHOUT the lock (state.locks.on_unavailable="
                    "proceed)", detail)
        self._depth = 1
        self.held = False
        return False

    # -- release ------------------------------------------------------------- #
    def release(self) -> None:
        if self._depth > 1:
            self._depth -= 1
            return
        self._depth = 0
        fd, self._fd = self._fd, None
        self.held = False
        if fd is None:
            return
        try:
            if self.backend == "fcntl":
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            elif self.backend == "msvcrt":
                import msvcrt

                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:  # pragma: no cover - already released
                    pass
        finally:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover
                pass

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> bool:
        self.release()
        return False


@contextmanager
def file_lock(
    name: str,
    directory: str,
    *,
    backend: str = "auto",
    timeout_s: float = 120.0,
    on_unavailable: str = "proceed",
) -> Iterator[FileLock]:
    """``with file_lock("meta-tokens", cfg.state.locks.dir):``"""
    lock = FileLock(
        lock_path_for(name, directory),
        backend=backend,
        timeout_s=timeout_s,
        on_unavailable=on_unavailable,
    )
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


#: Same default root as ``[state] dir``. Named here rather than imported from
#: the config layer, because state sits BELOW config and must not import it.
_DEFAULT_LOCK_DIR = os.path.join("~", ".local", "state", "makervox_publish", "locks")


@dataclass(frozen=True)
class LockPolicy:
    """The ``[state.locks]`` block, resolved once and passed down.

    Every store on a host shares one policy, so "which backend, how long do we
    wait, and what happens when the lock is unavailable" is answered in exactly
    one place instead of per call site.
    """

    dir: str = _DEFAULT_LOCK_DIR
    backend: str = "auto"
    acquire_timeout_s: float = 120.0
    on_unavailable: str = "proceed"
    dir_mode: int = 0o700

    @classmethod
    def from_config(cls, state_cfg: Any) -> "LockPolicy":
        """Build from a :class:`makervox_publish.config.StateConfig` (or anything alike)."""
        locks = getattr(state_cfg, "locks", None)
        state_dir = getattr(state_cfg, "dir", None)
        default_dir = under(state_dir, "locks") if state_dir else _DEFAULT_LOCK_DIR
        if locks is None:
            return cls(dir=default_dir,
                       dir_mode=int(getattr(state_cfg, "dir_mode", 0o700)))
        return cls(
            dir=getattr(locks, "dir", None) or default_dir,
            backend=getattr(locks, "backend", "auto"),
            acquire_timeout_s=float(getattr(locks, "acquire_timeout_s", 120.0)),
            on_unavailable=getattr(locks, "on_unavailable", "proceed"),
            dir_mode=int(getattr(state_cfg, "dir_mode", 0o700)),
        )

    def lock(self, name: str, *, timeout_s: Optional[float] = None) -> FileLock:
        return FileLock(
            lock_path_for(name, self.dir),
            backend=self.backend,
            timeout_s=self.acquire_timeout_s if timeout_s is None else timeout_s,
            on_unavailable=self.on_unavailable,
            dir_mode=self.dir_mode,
        )


@contextmanager
def locked(
    policy: Optional[LockPolicy],
    name: str,
    *,
    timeout_s: Optional[float] = None,
) -> Iterator[bool]:
    """Hold the named lock for the block, yielding whether it was really held.

    The yielded flag is not decoration. Under ``on_unavailable = "proceed"`` a
    caller can be running unlocked, and code about to spend a rotating refresh
    token needs to be able to say so in its logs rather than assume it is alone.

    ``policy`` may be None for callers that have no state config to hand; that
    resolves to the default lock directory rather than to "no locking", because
    silently running unlocked is the failure this module exists to prevent.
    """
    lock = (policy or LockPolicy()).lock(name, timeout_s=timeout_s)
    held = lock.acquire()
    try:
        yield held
    finally:
        lock.release()
