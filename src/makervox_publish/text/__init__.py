"""makervox_publish.text — pure text transforms. No I/O, no platform code, no network.

Everything here is a function of its input, which is what makes the ordering
rules testable: the caption-shaping and link-stripping bugs this package carries
regression tests for were all ORDER bugs, and an order bug is invisible to a
test that only inspects the final string.
"""

from __future__ import annotations

from makervox_publish.text.links import (
    URL_RE,
    LinkTagger,
    NoopTagger,
    UtmTagger,
    readable_text,
    strip_from_config,
    strip_link_lines,
)
from makervox_publish.text.shape import (
    DEFAULT_TRAILING_CONNECTORS,
    ShapeTrace,
    fingerprint,
    shape,
    shape_trace,
    shape_with_config,
    strip_dangling,
    truncate,
)

__all__ = [
    "LinkTagger",
    "NoopTagger",
    "UtmTagger",
    "strip_link_lines",
    "strip_from_config",
    "readable_text",
    "URL_RE",
    # caption shaping — pure, and traceable mid-pipeline because the bug it
    # guards against is an ORDER bug whose final string still looks right
    "shape",
    "shape_trace",
    "shape_with_config",
    "ShapeTrace",
    "strip_dangling",
    "truncate",
    "fingerprint",
    "DEFAULT_TRAILING_CONNECTORS",
]
