"""makervox_publish.platforms — the publishers, and the one result shape they share.

Layering: ``config / credentials / state / media / text / identity / http`` sit
BELOW this package and never import from it. Nothing here is imported by
``makervox_publish/__init__.py``, so a caller who only wants the config loader does not
pay for ``requests`` or for a platform they do not use.

There are NO lazy imports inside the platform modules. Lazy imports were
load-bearing in the code this was extracted from — Facebook imported Instagram
for a cover-frame helper, Instagram imported Facebook back for Page credentials,
and only function-level imports kept the cycle from exploding at import time.
Both halves of that cycle were fixed by MOVING code, not by deferring it: the
cover picker went down to :mod:`makervox_publish.media.cover`, and the Page lookup became
a resolver both publishers depend on and neither owns. Facebook and Instagram
then ship in ONE subpackage (:mod:`makervox_publish.platforms.meta`) because they share
one developer app, one Graph version, one token file and one lock — splitting
them is what forced the cycle in the first place.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

__all__ = ["PublishResult", "PLATFORMS", "load_platform"]

#: Module path per platform. Imported on demand by :func:`load_platform` so that
#: an optional dependency of one platform cannot break the others.
PLATFORMS = {
    "facebook": "makervox_publish.platforms.meta.facebook",
    "instagram": "makervox_publish.platforms.meta.instagram",
    "tiktok": "makervox_publish.platforms.tiktok.client",
    "x": "makervox_publish.platforms.x.client",
}


@dataclass(frozen=True)
class PublishResult:
    """What every publish call returns. It NEVER raises for an expected outcome.

    ``ok=False`` covers "not configured", "not linked" and "the platform said
    no" — all things a batch publisher must survive so that one dark account
    does not abort three working ones.

    ``duplicate=True`` comes back with ``ok=True`` on purpose: the post IS live,
    which is what the caller was actually asking. Reporting a suppressed
    duplicate as a failure sends a delivery alert about a post that exists.
    """

    ok: bool
    platform: str = ""
    account: str = ""
    #: The platform's id for the uploaded media (a video id, a container id).
    media_id: str = ""
    #: The platform's id for the resulting post/story, when it differs.
    post_id: str = ""
    reason: str = ""
    duplicate: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_tuple(self):
        """``(ok, info)`` — the shape the original callers branch on."""
        return self.ok, (self.media_id or self.post_id or self.reason)

    def __bool__(self) -> bool:
        return bool(self.ok)


def load_platform(name: str) -> Any:
    """Import one platform module by name.

    Deferred to call time rather than import time: the staging backends and
    cloud SDKs some platforms can use are optional extras, and a missing extra
    must not stop the other platforms from loading.
    """
    try:
        module_path = PLATFORMS[name]
    except KeyError:
        raise KeyError("unknown platform {0!r}; known: {1}".format(
            name, sorted(PLATFORMS)
        ))
    return importlib.import_module(module_path)


def publisher_for(name: str, *args, **kwargs) -> Optional[Any]:
    """Construct a platform's publisher, if that module exposes one."""
    module = load_platform(name)
    factory = getattr(module, "publisher", None)
    if factory is None:
        return None
    return factory(*args, **kwargs)
