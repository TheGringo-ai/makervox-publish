"""Pure caption shaping: no credentials, no network, no config file.

Everything here is a function of its arguments, because the shaping rules are
the part most likely to drift and the part hardest to see drift in. The whole
pipeline is also exposed as a TRACE (:func:`shape_trace`) rather than only its
output, for one specific reason spelled out below: the ordering bug this module
guards against still produces a plausible-looking final string, so a test that
inspects only the result cannot catch it.

Nothing in here is X-specific by construction — the caps and the connector list
are arguments — but the defaults are sized for a 280-character platform.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

__all__ = [
    "HASHTAG_RE",
    "URL_RE",
    "DEFAULT_TRAILING_CONNECTORS",
    "TRAILING_PUNCTUATION",
    "SENTENCE_END",
    "ShapeTrace",
    "shape",
    "shape_trace",
    "shape_with_config",
    "strip_dangling",
    "hashtags_in",
    "fingerprint",
    "truncate",
]

HASHTAG_RE = re.compile(r"#\w+")
URL_RE = re.compile(r"https?://\S+")

#: Words that only ever POINTED at the link. Once the URL is gone they are the
#: tail of a sentence with no object, so they come off from the right one token
#: at a time. A SET beats an ever-growing regex here: the failure mode is a
#: caption phrasing nobody anticipated, and adding a word is the whole fix.
#: English-only — pass your own list for another locale.
DEFAULT_TRAILING_CONNECTORS = (
    "at", "to", "on", "in", "via", "here", "now", "up", "is", "the", "link",
    "bio", "more", "read", "reading", "full", "story", "details", "detail",
    "see", "check", "it", "out", "below",
)

#: Separators and arrows that introduced the link. Kept OUT of the configurable
#: word list because they are not language-specific: a caption in any language
#: that ends "… →" is dangling for the same reason.
TRAILING_PUNCTUATION = ("→", "->", "=>", ":", "·", "|", "-", "—", "–", "»", ">", "•")

SENTENCE_END = (".", "!", "?", "…", '"', "'")


def hashtags_in(text: str) -> Tuple[str, ...]:
    """Every ``#tag`` in ``text``, in order of appearance."""
    return tuple(HASHTAG_RE.findall(text or ""))


def strip_dangling(text: str, connectors: Iterable[str] = DEFAULT_TRAILING_CONNECTORS) -> str:
    """Drop the connector phrase that a removed URL used to complete.

    Removing a URL strips the destination but leaves the phrase that pointed at
    it — "Full reading at", "Link in bio", "Read more →" — hanging off the end.
    This walks tokens from the right and stops at the first one that is real
    content or that ends a sentence, so "Read the cards at dawn" keeps its "at":
    only a TRAILING connector goes.
    """
    words = {str(c).lower() for c in connectors}
    words.update(p.lower() for p in TRAILING_PUNCTUATION)
    parts = (text or "").split()
    while parts:
        token = parts[-1].strip("".join(SENTENCE_END) + ",;")
        # A token that ENDS a sentence is content even if the word matches:
        # "…and that is it." must not lose "it."
        if parts[-1].endswith(SENTENCE_END) or token.lower() not in words:
            break
        parts.pop()
    return " ".join(parts)


def truncate(text: str, *, max_chars: int = 280, truncate_to: int = 277,
             min_sentence_boundary: int = 180) -> str:
    """Cut to length, preferring a sentence boundary over a mid-word chop."""
    if len(text) <= max_chars:
        return text
    cut = text[:truncate_to]
    for separator in (". ", "! ", "? ", "\n"):
        index = cut.rfind(separator)
        if index > min_sentence_boundary:
            return cut[:index + 1].strip()
    return cut.rsplit(" ", 1)[0] + "…"


@dataclass(frozen=True)
class ShapeTrace:
    """Every intermediate stage of :func:`shape`, so tests can assert MID-pipeline.

    The ordering defect this exists to catch (hashtags removed AFTER the
    dangling sweep instead of before) still yields a final string that ends in
    hashtags and looks fine. ``after_hashtags`` and ``after_dangling`` are where
    it is visible.
    """

    source: str
    after_urls: str
    hashtags: Tuple[str, ...]
    after_hashtags: str
    after_dangling: str
    final: str

    def __str__(self) -> str:
        return self.final


def shape_trace(
    caption: str,
    *,
    max_chars: int = 280,
    truncate_to: Optional[int] = None,
    min_sentence_boundary: int = 180,
    max_hashtags: int = 2,
    strip_urls: bool = True,
    trailing_connectors: Sequence[str] = DEFAULT_TRAILING_CONNECTORS,
) -> ShapeTrace:
    """Shape a caption and return every stage of the transformation."""
    source = caption or ""
    limit = max_chars if truncate_to is None else truncate_to

    after_urls = URL_RE.sub("", source) if strip_urls else source

    # ORDER IS LOAD-BEARING. Hashtags come out BEFORE the dangling-CTA sweep,
    # not after. Captions put their tags after the link, so with the tags still
    # present the leftover "Full reading at" sits MID-string rather than at the
    # end, the right-anchored sweep below never fires, and the post ships ending
    # in a preposition. Reversing these two lines still passes any test that
    # only inspects the final string, because that ends in hashtags either way.
    tags = hashtags_in(after_urls)
    after_hashtags = HASHTAG_RE.sub("", after_urls)
    after_hashtags = re.sub(r"[ \t]+", " ", after_hashtags)

    after_dangling = strip_dangling(after_hashtags, trailing_connectors)
    text = re.sub(r"\n{3,}", "\n\n", after_dangling).strip(" \n-·|")

    kept = list(tags[:max_hashtags]) if max_hashtags > 0 else []
    if kept:
        # Re-attach only if the tags still fit; a caption that would blow the
        # limit keeps its body and loses the decoration, never the reverse.
        candidate = text + "\n\n" + " ".join(kept)
        text = candidate if len(candidate) <= max_chars else text

    final = truncate(
        text,
        max_chars=max_chars,
        truncate_to=limit,
        min_sentence_boundary=min_sentence_boundary,
    ).strip()

    return ShapeTrace(
        source=source,
        after_urls=after_urls,
        hashtags=tags,
        after_hashtags=after_hashtags,
        after_dangling=after_dangling,
        final=final,
    )


def shape(caption: str, **kwargs) -> str:
    """Caption -> post body: no URL, capped hashtags, within the character limit.

    Exposed and pure so the shaping rules can be tested without credentials or a
    network call.
    """
    return shape_trace(caption, **kwargs).final


def shape_with_config(caption: str, cfg) -> str:
    """Shape using a :class:`makervox_publish.config.schema.XShapeConfig`."""
    return shape_trace(
        caption,
        max_chars=cfg.max_chars,
        truncate_to=cfg.truncate_to,
        min_sentence_boundary=cfg.min_sentence_boundary,
        max_hashtags=cfg.max_hashtags,
        strip_urls=cfg.strip_urls,
        trailing_connectors=cfg.trailing_connectors or DEFAULT_TRAILING_CONNECTORS,
    ).final


def fingerprint(text: str, limit: int = 160) -> str:
    """Lowercased alphanumerics only.

    Emoji and punctuation churn must not make two near-identical captions look
    distinct to a duplicate check — "This is your sign ✨" and "This is your
    sign!" are the same post.
    """
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())[:limit]
