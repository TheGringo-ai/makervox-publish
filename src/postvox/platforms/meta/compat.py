"""Every API-version-sensitive sniff, in ONE file.

WHY THEY ARE ALL HERE
---------------------
Each of these reads a response shape that the vendor never promised and can
change between Graph versions. None of them fails loudly when it drifts: they
fail as "no cover", "no first comment", "no views" — silent degradations that
look like a reach problem rather than a bug. The only way to notice a drift is
to have the observed response shapes written down in one auditable place, so
when the version is bumped there is a single file to re-verify.

The observed shapes are documented against Graph v21.0.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

__all__ = [
    "graph_error",
    "error_message",
    "is_already_published",
    "is_permission_error",
    "upload_succeeded",
    "thumbnail_accepted",
    "photo_post_id",
    "story_object_id",
    "insight_value",
]


def graph_error(payload: Any) -> Optional[Dict[str, Any]]:
    """The ``error`` object of a Graph response, if there is one."""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return error
    return None


def error_message(payload: Any, limit: int = 400) -> str:
    """A one-line rendering of a Graph error for a log record."""
    error = graph_error(payload)
    if error is None:
        return str(payload)[:limit]
    bits = [str(error.get("message") or "")]
    for field in ("type", "code", "error_subcode", "error_user_title", "error_user_msg"):
        value = error.get(field)
        if value not in (None, ""):
            bits.append("{0}={1}".format(field, value))
    return " ".join(b for b in bits if b).strip()[:limit]


def is_already_published(payload: Any) -> bool:
    """True when a re-finish hit a video the platform already committed.

    A substring sniff, deliberately. There is no stable error code for it, and
    the alternative — treating it as a failure — makes the retry mint a NEW
    upload and publish a SECOND copy of the same reel, which is the exact
    outcome the retry exists to avoid.

    Observed (v21.0): the error message contains both "already" and "publish"
    ("...has already been published...").
    """
    text = str(payload).lower()
    return "already" in text and "publish" in text


def is_permission_error(payload: Any) -> bool:
    """True for "(#200) ... insufficient permission" on the Page token.

    Comments need ``pages_manage_engagement``. Without it the comment fails and
    the POST that created the reel still succeeded — so this must never be
    treated as a publish failure.
    """
    error = graph_error(payload) or {}
    if error.get("code") == 200:
        return True
    text = str(error.get("message") or "").lower()
    return "permission" in text and ("insufficient" in text or "missing" in text)


def upload_succeeded(payload: Any) -> bool:
    """Whether a resumable upload response means success.

    Observed (v21.0): a successful upload answers ``{"success": true}``, but
    some responses omit the key entirely and are still fine. A MISSING ``success``
    key therefore means OK; only an explicit falsy ``success`` ALONGSIDE a
    ``debug_info`` is a real failure. Reading a missing key as failure would fail
    every good upload.
    """
    if not isinstance(payload, dict):
        return True
    if graph_error(payload) is not None:
        return False
    if payload.get("success", True):
        return True
    return "debug_info" not in payload


def thumbnail_accepted(payload: Any) -> bool:
    """Whether a preferred-thumbnail upload was accepted.

    Observed (v21.0): this endpoint answers ``{"success": true}`` — it does NOT
    return an id, so checking for an id alone rejects every successful call.
    """
    if not isinstance(payload, dict):
        return False
    if graph_error(payload) is not None:
        return False
    return bool(payload.get("success")) or "id" in payload


def photo_post_id(payload: Any) -> str:
    """The id of a published photo.

    Observed (v21.0): the response carries ``id`` (the photo) and usually
    ``post_id`` (the story). ``post_id`` is the one worth returning — it is what
    a human sees in the feed — but either alone counts as success.
    """
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("post_id") or payload.get("id") or "")


def story_object_id(page_id: str, video_node: Any) -> str:
    """The COMMENTABLE object for a published reel: ``{page_id}_{story_fbid}``.

    This is the single most surprising shape in the Meta surface. The reel
    finish phase returns a ``post_id`` that is NOT commentable: posting to
    ``{page_id}_{video_id}`` or to the bare video id returns
    ``(#100) Unsupported post request ... Object ... does not exist``.

    The real object is ``{page_id}_{story_fbid}``, where the story fbid is the
    VIDEO NODE'S OWN ``post_id`` FIELD — fetched off ``GET /{video_id}
    ?fields=post_id``, not from the finish response. Right after publish that
    field can lag a beat, so the caller retries the FETCH; and even once it
    resolves, the POST itself still has to be retried (see the first-comment
    race in :mod:`postvox.platforms.meta.facebook`).
    """
    if not isinstance(video_node, dict):
        return ""
    story = video_node.get("post_id")
    if not story:
        return ""
    story = str(story)
    # Some responses already return the composite form. Do not double-prefix it.
    if "_" in story:
        return story
    return "{0}_{1}".format(page_id, story)


def insight_value(payload: Any, metrics: Sequence[str] = ()) -> Optional[int]:
    """The largest value across the reel play metrics in an insights response.

    Reels have NO impressions/reach metric in this API version; plays are the
    proxy and they come from ``/video_insights``, not from the ``views`` field
    on the video node. Different metric names are populated for different
    videos, so the maximum across them is the honest single number.
    """
    if not isinstance(payload, dict):
        return None
    wanted = set(metrics or ())
    best = None
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        if wanted and item.get("name") not in wanted:
            continue
        values = item.get("values") or [{}]
        raw = values[0].get("value") if isinstance(values[0], dict) else None
        try:
            number = int(raw)
        except (TypeError, ValueError):
            continue
        if best is None or number > best:
            best = number
    return best
