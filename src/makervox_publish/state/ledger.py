"""The delivery ledger — an append-only record of what actually shipped.

THIS IS PUBLIC API, NOT AN INTERNAL FILE.

In the system this was extracted from, four separate tools read and wrote these
rows: a recovery job that re-posts anything missing, a health check, a
per-creative performance report, and the slot assigner. Extracting the publisher
alone would have left all four pointed at a schema they no longer controlled. So
the reader, the writer and the row shape ship together here, the schema is
versioned, and new fields are additive only.

WHAT A ROW MEANS
----------------
"This exact content was delivered to this account on this date." The identity is
``(account, date, key, slot, sha)`` — NOT the filename. See
``makervox_publish.identity.post_id`` for why a filename is not an identity.

v1 COMPATIBILITY
----------------
The original rows had no ``sha`` and no ``slot``, and named their columns
``brand``/``video_id``. Those rows are READ and treated as "identity unknown":
they can be listed and reported on, but they can never match a fingerprint, so a
dedupe check against them fails OPEN (publishes) rather than silently
suppressing a real post. Migrating is not required.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional

from makervox_publish.logging import get_logger
from makervox_publish.state.paths import chmod_quietly, ensure_parent, expand
from makervox_publish.version import LEDGER_SCHEMA_VERSION

__all__ = ["LedgerRow", "Ledger", "JsonlLedger"]

log = get_logger(__name__)


@dataclass(frozen=True)
class LedgerRow:
    """One delivery. Field names are frozen; new fields go on the end."""

    account: str
    platform: str = "facebook"
    #: The platform's id for the uploaded media (a video_id, a container id).
    media_id: str = ""
    #: The platform's id for the resulting post/story, when it differs.
    post_id: str = ""
    #: Basename of the SOURCE file, with the slot marker intact.
    base: str = ""
    #: ISO date this post belongs to, parsed from the identity.
    date: str = ""
    #: The creative type. Two slots of the same type share one key on purpose:
    #: they must land on ONE creative in attribution, not split into two.
    key: str = ""
    #: Which of the day's slots this was. Unmarked rows are slot 1.
    slot: int = 1
    #: Short content fingerprint of the SOURCE media. "" means unknown.
    sha: str = ""
    ts: float = 0.0
    schema_version: int = LEDGER_SCHEMA_VERSION
    extra: Dict[str, Any] = field(default_factory=dict)

    # -- serialization ------------------------------------------------------- #
    def to_json(self) -> Dict[str, Any]:
        row = {
            "v": int(self.schema_version),
            "ts": float(self.ts or time.time()),
            "platform": self.platform,
            "account": self.account,
            "media_id": self.media_id,
            "post_id": self.post_id,
            "base": self.base,
            "date": self.date,
            "key": self.key,
            "slot": int(self.slot),
            "sha": self.sha,
        }
        row.update(self.extra or {})
        return row

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "LedgerRow":
        """Parse a row, tolerating the v1 column names.

        v1 said ``brand``/``video_id`` and carried neither ``sha`` nor ``slot``.
        Those become account/media_id with an empty sha, which reads downstream
        as "identity unknown" — exactly what it is.
        """
        known = ("v", "ts", "platform", "account", "media_id", "post_id", "base",
                 "date", "key", "slot", "sha", "brand", "video_id")
        version = int(data.get("v") or 1)
        return cls(
            account=str(data.get("account") or data.get("brand") or ""),
            platform=str(data.get("platform") or "facebook"),
            media_id=str(data.get("media_id") or data.get("video_id") or ""),
            post_id=str(data.get("post_id") or ""),
            base=str(data.get("base") or ""),
            date=str(data.get("date") or ""),
            key=str(data.get("key") or ""),
            slot=_int(data.get("slot"), 1),
            sha=str(data.get("sha") or ""),
            ts=float(data.get("ts") or 0.0),
            schema_version=version,
            extra={k: v for k, v in data.items() if k not in known},
        )

    @property
    def identity_known(self) -> bool:
        """A row without a fingerprint can never prove it is the same post."""
        return bool(self.sha and self.date)


class Ledger:
    """The interface. Implement these four methods to swap the backend.

    Shipped implementations: :class:`JsonlLedger` (the default; one append-only
    file, readable by anything) and — for multi-process hosts where several
    writers append at once — a SQLite backend with the same surface.
    """

    def append(self, row: LedgerRow) -> LedgerRow:  # pragma: no cover - interface
        raise NotImplementedError

    def rows(self, *, account: Optional[str] = None, platform: Optional[str] = None,
             date: Optional[str] = None) -> List[LedgerRow]:  # pragma: no cover
        raise NotImplementedError

    def find_delivered(self, account: str, date: str, sha: str, *,
                       platform: Optional[str] = None) -> Optional[LedgerRow]:
        # pragma: no cover - interface
        raise NotImplementedError

    def __iter__(self) -> Iterator[LedgerRow]:  # pragma: no cover - interface
        return iter(self.rows())


class JsonlLedger(Ledger):
    """One JSON object per line. The default, and deliberately boring.

    Append-only, so a crashed writer can lose at most the row it was writing,
    and any other tool can read the file with ``json.loads`` per line. A row
    that fails to parse is SKIPPED WITH A WARNING rather than aborting the read:
    one corrupt line must not make an entire delivery history unreadable, but it
    must also not vanish silently.
    """

    def __init__(
        self,
        path: str,
        *,
        schema_version: int = LEDGER_SCHEMA_VERSION,
        file_mode: int = 0o600,
        dir_mode: int = 0o700,
        enforce_file_mode: bool = True,
    ) -> None:
        self.path = expand(path)
        self.schema_version = int(schema_version)
        self.file_mode = int(file_mode)
        self.dir_mode = int(dir_mode)
        self.enforce_file_mode = bool(enforce_file_mode)

    @classmethod
    def from_config(cls, ledger_cfg, state_cfg=None) -> "JsonlLedger":
        """Build from a :class:`makervox_publish.config.LedgerConfig` (+ ``[state]``)."""
        options = dict(getattr(ledger_cfg, "options", {}) or {})
        path = options.get("path")
        if not path:
            state_dir = getattr(state_cfg, "dir", "") or "."
            path = os.path.join(state_dir, "delivered.jsonl")
        return cls(
            path,
            schema_version=int(getattr(ledger_cfg, "schema_version", LEDGER_SCHEMA_VERSION)),
            file_mode=int(getattr(state_cfg, "file_mode", 0o600)),
            dir_mode=int(getattr(state_cfg, "dir_mode", 0o700)),
            enforce_file_mode=bool(getattr(state_cfg, "enforce_file_mode", True)),
        )

    # -- writing ------------------------------------------------------------- #
    def append(self, row: LedgerRow) -> LedgerRow:
        """Append one delivery. The ONLY method here that creates anything.

        Append mode, not read-modify-write: the ledger is append-only precisely
        so a concurrent writer cannot lose another's rows, and so a crash costs
        at most the line being written.
        """
        if not row.ts:
            row = _replace_ts(row)
        payload = row.to_json()
        payload["v"] = self.schema_version
        ensure_parent(self.path, self.dir_mode)
        fresh = not os.path.exists(self.path)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        if fresh:
            chmod_quietly(self.path, self.file_mode, enforce=self.enforce_file_mode)
        return row

    # -- reading ------------------------------------------------------------- #
    def rows(self, *, account: Optional[str] = None, platform: Optional[str] = None,
             date: Optional[str] = None) -> List[LedgerRow]:
        out = []  # type: List[LedgerRow]
        for row in self._iter_file():
            if account is not None and row.account != account:
                continue
            if platform is not None and row.platform != platform:
                continue
            if date is not None and row.date != date:
                continue
            out.append(row)
        return out

    def find_delivered(self, account: str, date: str, sha: str, *,
                       platform: Optional[str] = None) -> Optional[LedgerRow]:
        """The row this exact content was already delivered under, or None.

        Matches on (account, date, CONTENT) and deliberately not on filename.
        An empty ``sha`` never matches: no fingerprint means "cannot prove this
        is the same post", and the caller fails OPEN.
        """
        if not sha or not date:
            return None
        for row in self._iter_file():
            if row.account != account or row.date != date:
                continue
            if platform is not None and row.platform != platform:
                continue
            if row.sha and row.sha == sha:
                return row
        return None

    def _iter_file(self) -> Iterable[LedgerRow]:
        # Reads NEVER create the file or its directory. A publisher asking "did
        # I already send this?" on a fresh machine must not mutate $HOME.
        try:
            handle = open(self.path, "r", encoding="utf-8")
        except OSError:
            return
        bad = 0
        with handle:
            for number, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    bad += 1
                    if bad <= 3:
                        log.warning("ledger %s line %d is not valid JSON; skipping it",
                                    self.path, number)
                    continue
                if not isinstance(data, dict):
                    continue
                yield LedgerRow.from_json(data)
        if bad > 3:
            log.warning("ledger %s: %d unparseable lines skipped in total", self.path, bad)

    def __repr__(self) -> str:
        return "<JsonlLedger {0}>".format(self.path)


def _replace_ts(row: LedgerRow) -> LedgerRow:
    return LedgerRow(
        account=row.account, platform=row.platform, media_id=row.media_id,
        post_id=row.post_id, base=row.base, date=row.date, key=row.key,
        slot=row.slot, sha=row.sha, ts=time.time(),
        schema_version=row.schema_version, extra=dict(row.extra or {}),
    )


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
