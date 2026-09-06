"""Facebook and Instagram, shipped in ONE subpackage on purpose.

They are one Meta developer app, one Graph API version, one token file and one
lock. Splitting them into separate modules is what forced the circular import in
the code this was extracted from: Instagram needed the Facebook module's Page
credentials, Facebook needed the Instagram module's cover-frame picker, and both
edges were papered over with function-level imports.

The fix is layering, not lazy imports:

* the cover picker moved DOWN to :mod:`makervox_publish.media.cover`, which both import;
* Page credentials became :class:`~makervox_publish.platforms.meta.tokens.PageResolver`,
  a Protocol living in :mod:`makervox_publish.platforms.meta.tokens` that both depend on
  and neither owns.

Nothing in this package imports a sibling publisher.
"""

from __future__ import annotations

from makervox_publish.platforms.meta.tokens import (
    MetaTokenStore,
    PageCredentials,
    PageResolver,
    StoredPageResolver,
)

__all__ = [
    "MetaTokenStore",
    "PageCredentials",
    "PageResolver",
    "StoredPageResolver",
]
