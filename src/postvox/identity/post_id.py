"""Post identity: what makes two uploads "the same post".

A FILENAME IS NOT AN IDENTITY. The identity is
``(account, date, key, slot, content fingerprint)``. The filename can *suggest*
the first four; only the fingerprint settles the question.

THE SLOT MARKER
---------------
``__s<N>`` marks which of the day's slots a file came from. It exists because a
rotation can render the same content TYPE twice in one day and every type names
its file ``<type>_<date>.ext`` — so the second render silently overwrote the
first. That cost the post AND broke a downstream day-count that expected four
distinct files and only ever saw three, so it retried forever and published
duplicates. A double underscore plus ``s`` cannot be confused with a real
suffix (``_scorpio``, ``_three-of-cups``, ``_prayer``).

The marker is stripped FIRST, before the key is extracted, so
``sign_2031-04-09__s2.mp4`` still reports key ``sign``. Two slots of the same
type must land on ONE creative in performance reporting, not split into "sign"
and something like "s2" — the whole point of the marker is that it is invisible
to attribution. Unmarked files are slot 1, which keeps every pre-marker filename
and ledger row parsing exactly as before.

FAILING TO RESOLVE IS LOUD NOW
------------------------------
The original failed open SILENTLY when a filename carried no date, which meant
every adopter whose files were named differently got duplicate detection — the
most valuable behaviour in the package — disabled with no warning at all. Here
``on_unresolved`` still defaults to failing open (a rare duplicate beats
silently suppressing a real post) but it SAYS SO at WARNING, and ``refuse`` is
available for anyone who would rather stop.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from postvox.errors import PublishError
from postvox.identity.fingerprint import DEFAULT_HEX_CHARS, content_sha
from postvox.logging import get_logger

__all__ = [
    "PostIdentity",
    "PostIdentityResolver",
    "FilenamePostIdentity",
    "ExplicitPostIdentity",
    "resolve_identity",
]

log = get_logger(__name__)

#: The default grammar: "<key>_<YYYY-MM-DD>[__s<N>].<ext>".
DEFAULT_DATE_PATTERN = r"(\d{4}-\d{2}-\d{2})"
DEFAULT_SLOT_PATTERN = r"__s(\d+)(?=\.[^.]*$|$)"


@dataclass(frozen=True)
class PostIdentity:
    """Everything needed to say "I already sent this"."""

    account: str
    date: str = ""
    key: str = ""
    slot: int = 1
    sha: str = ""
    base: str = ""
    #: The file the fingerprint was taken from. This is the SOURCE media, not
    #: the temp cut that goes over the wire.
    source_path: str = ""

    @property
    def resolved(self) -> bool:
        """True when this identity can actually prove a duplicate.

        Both halves are required: a fingerprint with no date cannot be scoped to
        a day, and a date with no fingerprint cannot tell two different posts
        apart.
        """
        return bool(self.sha and self.date)

    def unresolved_reason(self) -> str:
        if not self.sha and not self.date:
            return "no content fingerprint and no date"
        if not self.sha:
            return "no content fingerprint (the source file could not be read)"
        return "no date could be parsed from {0!r}".format(self.base or self.source_path)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "account": self.account, "date": self.date, "key": self.key,
            "slot": self.slot, "sha": self.sha, "base": self.base,
        }


class PostIdentityResolver:
    """The interface: ``for_media(path, account, **ctx) -> PostIdentity``."""

    def for_media(self, path: str, account: str, **ctx: Any) -> PostIdentity:
        # pragma: no cover - interface
        raise NotImplementedError


class FilenamePostIdentity(PostIdentityResolver):
    """Derive date/key/slot from the filename, fingerprint from the bytes.

    This is a PRESET, not an assumption the package makes about you: point
    ``identity.impl`` at your own resolver, or pass a :class:`PostIdentity`
    explicitly at the call site, if your filenames do not carry a date.
    """

    def __init__(
        self,
        date_pattern: str = DEFAULT_DATE_PATTERN,
        slot_pattern: str = DEFAULT_SLOT_PATTERN,
        default_slot: int = 1,
        algorithm: str = "sha256",
        hex_chars: int = DEFAULT_HEX_CHARS,
    ) -> None:
        self.date_re = re.compile(date_pattern)
        self.slot_re = re.compile(slot_pattern)
        self.default_slot = int(default_slot)
        self.algorithm = algorithm
        self.hex_chars = int(hex_chars)

    @classmethod
    def from_config(cls, identity_cfg) -> "FilenamePostIdentity":
        options = dict(getattr(identity_cfg, "options", {}) or {})
        fingerprint = getattr(identity_cfg, "fingerprint", None)
        return cls(
            date_pattern=str(options.get("date_pattern", DEFAULT_DATE_PATTERN)),
            slot_pattern=str(options.get("slot_pattern", DEFAULT_SLOT_PATTERN)),
            default_slot=int(options.get("default_slot", 1)),
            algorithm=str(getattr(fingerprint, "algorithm", "sha256")),
            hex_chars=int(getattr(fingerprint, "hex_chars", DEFAULT_HEX_CHARS)),
        )

    # -- parsing -------------------------------------------------------------- #
    def split_slot(self, base: str):
        """``('sign_2031-04-09__s2.mp4')`` -> ``('sign_2031-04-09.mp4', 2)``."""
        match = self.slot_re.search(base)
        if not match:
            return base, self.default_slot
        try:
            slot = int(match.group(1))
        except (IndexError, ValueError):
            slot = self.default_slot
        return base[: match.start()] + base[match.end():], slot

    def parse(self, base: str):
        """``(date, key, slot)`` from a filename.

        ``dailyverse_2031-04-09_prayer.mp4`` -> ``('2031-04-09', 'prayer', 1)``
        ``oracle_2031-04-09.mp4``            -> ``('2031-04-09', 'oracle', 1)``

        The slot marker is stripped FIRST so both slots of a type report one key.
        """
        base, slot = self.split_slot(base)
        match = self.date_re.search(base)
        date = match.group(1) if match else ""
        stem = os.path.splitext(base)[0]
        after = stem.split(date)[-1].strip("_") if date else ""
        key = after or (stem.split("_")[0] if "_" in stem else stem)
        return date, key, slot

    def for_media(self, path: str, account: str, **ctx: Any) -> PostIdentity:
        source = str(ctx.get("source_path") or path)
        base = os.path.basename(source)
        date, key, slot = self.parse(base)
        return PostIdentity(
            account=account,
            date=date,
            key=key,
            slot=slot,
            # The SOURCE file, never the temp cut on the wire.
            sha=content_sha(source, algorithm=self.algorithm, hex_chars=self.hex_chars),
            base=base,
            source_path=source,
        )

    def __repr__(self) -> str:
        return "<FilenamePostIdentity date={0!r} slot={1!r}>".format(
            self.date_re.pattern, self.slot_re.pattern
        )


class ExplicitPostIdentity(PostIdentityResolver):
    """For callers who know the identity and refuse to guess from a filename.

    Recommended for anyone whose filenames do not carry a date: pass the date
    and key you already have, and let the fingerprint come from the bytes.
    """

    def __init__(self, date: str = "", key: str = "", slot: int = 1,
                 algorithm: str = "sha256", hex_chars: int = DEFAULT_HEX_CHARS) -> None:
        self.date = date
        self.key = key
        self.slot = int(slot)
        self.algorithm = algorithm
        self.hex_chars = int(hex_chars)

    def for_media(self, path: str, account: str, **ctx: Any) -> PostIdentity:
        source = str(ctx.get("source_path") or path)
        return PostIdentity(
            account=account,
            date=str(ctx.get("date") or self.date),
            key=str(ctx.get("key") or self.key),
            slot=int(ctx.get("slot") or self.slot),
            sha=content_sha(source, algorithm=self.algorithm, hex_chars=self.hex_chars),
            base=os.path.basename(source),
            source_path=source,
        )


def resolve_identity(
    resolver: Optional[PostIdentityResolver],
    path: str,
    account: str,
    *,
    identity: Optional[PostIdentity] = None,
    on_unresolved: str = "warn_and_publish",
    **ctx: Any
) -> Optional[PostIdentity]:
    """Produce an identity, applying the ``on_unresolved`` policy.

    Returns None when the identity cannot be resolved and policy says publish
    anyway; the caller then skips the dedupe check. ``refuse`` raises instead.
    """
    if identity is not None:
        resolved = identity
    elif resolver is None:
        resolved = None
    else:
        try:
            resolved = resolver.for_media(path, account, **ctx)
        except Exception as exc:  # a third-party resolver misbehaving
            log.warning(
                "post-identity resolver %r failed for %s: %s — duplicate "
                "detection is OFF for this post",
                type(resolver).__name__, path, exc,
            )
            resolved = None

    if resolved is not None and resolved.resolved:
        return resolved

    reason = resolved.unresolved_reason() if resolved else "no identity resolver configured"
    policy = (on_unresolved or "warn_and_publish").lower()
    if policy == "refuse":
        raise PublishError(
            "refusing to publish {0}: {1}. identity.on_unresolved is 'refuse'; "
            "pass an explicit PostIdentity or set it to 'warn_and_publish'."
            .format(path, reason)
        )
    if policy == "warn_and_publish":
        log.warning(
            "cannot establish a post identity for %s (%s) — publishing anyway "
            "with duplicate detection DISABLED for this post. A rare duplicate "
            "beats silently suppressing a real post, but this is why you might "
            "see one.", path, reason,
        )
    # Returned even when unresolved: the caller still records base/date/key in
    # the ledger. It MUST check `.resolved` before treating it as a duplicate
    # key — an unresolved identity can never match, by construction.
    return resolved
