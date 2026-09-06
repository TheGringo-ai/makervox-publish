"""makervox_publish.identity — what makes two uploads the same post.

    from makervox_publish.identity import FilenamePostIdentity, PostIdentity

    resolver = FilenamePostIdentity()
    identity = resolver.for_media("sign_2031-04-09__s2.mp4", account="moonlit")
    identity.key   # "sign"  — the slot marker is invisible to attribution
    identity.slot  # 2
    identity.sha   # fingerprint of the SOURCE bytes

A filename is not an identity; the fingerprint is. See
:mod:`makervox_publish.identity.fingerprint` for the incident behind that.
"""

from __future__ import annotations

from makervox_publish.identity.fingerprint import DEFAULT_HEX_CHARS, content_sha, sha_for
from makervox_publish.identity.post_id import (
    DEFAULT_DATE_PATTERN,
    DEFAULT_SLOT_PATTERN,
    ExplicitPostIdentity,
    FilenamePostIdentity,
    PostIdentity,
    PostIdentityResolver,
    resolve_identity,
)

__all__ = [
    "PostIdentity",
    "PostIdentityResolver",
    "FilenamePostIdentity",
    "ExplicitPostIdentity",
    "resolve_identity",
    "content_sha",
    "sha_for",
    "DEFAULT_HEX_CHARS",
    "DEFAULT_DATE_PATTERN",
    "DEFAULT_SLOT_PATTERN",
]
