"""Small, durable counters — daily caps and recent-post fingerprints.

This is the state behind the X posting governor, and it gates something that
SPENDS REAL MONEY PER CALL, so the concurrency story is written down rather than
assumed:

* :class:`JsonCounterStore` is an UNLOCKED read-modify-write. It is correct only
  when callers are serialized on one machine — which was true of the scheduler
  this came from and is NOT true of a library. Two concurrent callers each read
  ``count=0``, each write ``count=1``, and both post: the cap reads green while
  the account posts twice and is billed twice.
* :class:`SqliteCounterStore` does the same read-modify-write inside a single
  ``BEGIN IMMEDIATE`` transaction, so a second writer blocks instead of losing
  the increment. Use it for threads, worker processes, containers or serverless.

Both implement :class:`CounterStore`; anything else with these three methods
works too (``impl = "mypkg:MyStore"`` in the config file).
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Mapping, Optional

try:  # pragma: no cover - typing.Protocol exists on every supported version
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - Python < 3.8
    Protocol = object  # type: ignore[assignment]

    def runtime_checkable(cls):  # type: ignore[misc]
        return cls

from postvox.logging import get_logger
from postvox.state import atomic
from postvox.state.paths import chmod_quietly, ensure_parent, expand

__all__ = ["CounterStore", "JsonCounterStore", "SqliteCounterStore", "MemoryCounterStore"]

log = get_logger(__name__)

Mutator = Callable[[Dict[str, Any]], Dict[str, Any]]


@runtime_checkable
class CounterStore(Protocol):
    """A tiny keyed document store. Keys are opaque strings chosen by the caller."""

    def read(self, key: str) -> Dict[str, Any]:
        """Return the document for ``key``; ``{}`` when absent."""

    def write(self, key: str, value: Mapping[str, Any]) -> None:
        """Replace the document for ``key``."""

    def update(self, key: str, mutate: Mutator) -> Dict[str, Any]:
        """Read, apply ``mutate``, write, and return the stored document.

        Implementations that CAN make this atomic must do so. The mutator gets a
        private copy and must return the document to store.
        """

    def describe(self) -> str:
        """One line for ``postvox doctor``. Never includes a secret."""


class MemoryCounterStore:
    """In-process only. For tests and for callers who persist state themselves."""

    def __init__(self, initial: Optional[Mapping[str, Mapping[str, Any]]] = None) -> None:
        self._data = {k: dict(v) for k, v in (initial or {}).items()}
        self.name = "memory"

    def read(self, key: str) -> Dict[str, Any]:
        return dict(self._data.get(key) or {})

    def write(self, key: str, value: Mapping[str, Any]) -> None:
        self._data[key] = dict(value)

    def update(self, key: str, mutate: Mutator) -> Dict[str, Any]:
        updated = dict(mutate(self.read(key)) or {})
        self.write(key, updated)
        return updated

    def describe(self) -> str:
        return "in-memory counters ({0} keys, not persisted)".format(len(self._data))


class JsonCounterStore:
    """One JSON document holding every key. The zero-dependency default.

    ⚠️ UNLOCKED read-modify-write — see the module docstring. Correct on a single
    machine whose callers do not overlap; lossy anywhere else, and what it loses
    is a paid post's counter.
    """

    def __init__(
        self,
        path: str,
        *,
        file_mode: int = 0o600,
        dir_mode: int = 0o700,
        enforce_file_mode: bool = True,
    ) -> None:
        self.path = expand(path)
        self._file_mode = file_mode
        self._dir_mode = dir_mode
        self._enforce = enforce_file_mode
        self.name = "json:{0}".format(self.path)

    def _load_all(self) -> Dict[str, Any]:
        data = atomic.read_json(self.path, default={})
        return data if isinstance(data, dict) else {}

    def read(self, key: str) -> Dict[str, Any]:
        value = self._load_all().get(key)
        return dict(value) if isinstance(value, dict) else {}

    def write(self, key: str, value: Mapping[str, Any]) -> None:
        data = self._load_all()
        data[key] = dict(value)
        atomic.write_json(
            self.path, data,
            file_mode=self._file_mode,
            dir_mode=self._dir_mode,
            enforce_file_mode=self._enforce,
        )

    def update(self, key: str, mutate: Mutator) -> Dict[str, Any]:
        data = self._load_all()
        current = data.get(key)
        updated = dict(mutate(dict(current) if isinstance(current, dict) else {}) or {})
        data[key] = updated
        atomic.write_json(
            self.path, data,
            file_mode=self._file_mode,
            dir_mode=self._dir_mode,
            enforce_file_mode=self._enforce,
        )
        return updated

    def describe(self) -> str:
        return "json counters at {0} (single-writer only)".format(self.path)


class SqliteCounterStore:
    """The same store, serialized by the database.

    ``update`` runs inside ``BEGIN IMMEDIATE``, so a concurrent writer waits for
    the write lock instead of overwriting a count it never saw.
    """

    def __init__(
        self,
        path: str,
        *,
        table: str = "counters",
        timeout_s: float = 30.0,
        file_mode: int = 0o600,
        dir_mode: int = 0o700,
        enforce_file_mode: bool = True,
    ) -> None:
        import sqlite3  # stdlib, but lazily imported: some minimal builds omit it

        self._sqlite3 = sqlite3
        self.path = expand(path)
        # Identifiers cannot be parameterised in SQL, so the table name is
        # restricted rather than quoted-and-hoped.
        if not table.replace("_", "").isalnum():
            raise ValueError("table name must be alphanumeric/underscore, got {0!r}".format(table))
        self._table = table
        self._timeout = float(timeout_s)
        self._file_mode = file_mode
        self._dir_mode = dir_mode
        self._enforce = enforce_file_mode
        self.name = "sqlite:{0}".format(self.path)
        self._ready = False

    def _connect(self):
        created = not os.path.exists(self.path)
        ensure_parent(self.path, self._dir_mode)
        conn = self._sqlite3.connect(self.path, timeout=self._timeout, isolation_level=None)
        if created:
            chmod_quietly(self.path, self._file_mode, enforce=self._enforce)
        if not self._ready:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS {0} "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)".format(self._table)
            )
            self._ready = True
        return conn

    def read(self, key: str) -> Dict[str, Any]:
        import json

        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM {0} WHERE key = ?".format(self._table), (key,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {}
        try:
            value = json.loads(row[0])
        except ValueError:
            log.warning("counter row %r is not valid JSON; treating it as empty", key)
            return {}
        return dict(value) if isinstance(value, dict) else {}

    def write(self, key: str, value: Mapping[str, Any]) -> None:
        import json

        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO {0} (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value".format(self._table),
                (key, json.dumps(dict(value), ensure_ascii=False)),
            )
        finally:
            conn.close()

    def update(self, key: str, mutate: Mutator) -> Dict[str, Any]:
        import json

        conn = self._connect()
        try:
            # IMMEDIATE takes the write lock up front, so the read below cannot
            # be overtaken between here and the write. A deferred transaction
            # would upgrade only at write time and could fail with SQLITE_BUSY
            # after the mutator already ran.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM {0} WHERE key = ?".format(self._table), (key,)
            ).fetchone()
            current = {}  # type: Dict[str, Any]
            if row:
                try:
                    loaded = json.loads(row[0])
                    current = dict(loaded) if isinstance(loaded, dict) else {}
                except ValueError:
                    log.warning("counter row %r is not valid JSON; treating it as empty", key)
            updated = dict(mutate(current) or {})
            conn.execute(
                "INSERT INTO {0} (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value".format(self._table),
                (key, json.dumps(updated, ensure_ascii=False)),
            )
            conn.execute("COMMIT")
            return updated
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # pragma: no cover - rollback on a dead handle
                pass
            raise
        finally:
            conn.close()

    def describe(self) -> str:
        return "sqlite counters at {0} (safe for concurrent writers)".format(self.path)
