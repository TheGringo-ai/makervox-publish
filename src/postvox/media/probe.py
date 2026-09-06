"""Duration and dimensions via ffprobe."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from postvox.errors import FfmpegFailed, MediaError
from postvox.logging import get_logger
from postvox.media.ffmpeg import FfmpegTools

__all__ = ["MediaInfo", "probe", "duration_s"]

log = get_logger(__name__)


@dataclass(frozen=True)
class MediaInfo:
    path: str
    size_bytes: int
    duration_s: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    has_audio: bool = False

    @property
    def is_portrait(self) -> bool:
        return bool(self.width and self.height and self.height > self.width)

    @property
    def aspect(self) -> Optional[float]:
        if not self.width or not self.height:
            return None
        return float(self.width) / float(self.height)


def probe(path: str, tools: FfmpegTools, *, timeout_s: float = 60.0) -> MediaInfo:
    """Inspect a media file. Raises MediaError when ffprobe cannot read it."""
    full = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(full):
        raise MediaError("no such media file: {0}".format(full))
    size = os.path.getsize(full)

    proc = tools.run(
        ["-print_format", "json", "-show_format", "-show_streams", full],
        timeout_s=timeout_s,
        binary="ffprobe",
    )
    try:
        data = json.loads((proc.stdout or b"{}").decode("utf-8", "replace"))
    except ValueError as exc:
        raise MediaError("ffprobe returned unparseable JSON for {0}: {1}".format(full, exc))

    duration = None
    fmt = data.get("format") or {}
    if fmt.get("duration") is not None:
        try:
            duration = float(fmt["duration"])
        except (TypeError, ValueError):
            duration = None

    width = height = None
    has_audio = False
    for stream in data.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "video" and width is None:
            width = _int_or_none(stream.get("width"))
            height = _int_or_none(stream.get("height"))
            if duration is None and stream.get("duration") is not None:
                try:
                    duration = float(stream["duration"])
                except (TypeError, ValueError):
                    pass
        elif kind == "audio":
            has_audio = True

    return MediaInfo(
        path=full,
        size_bytes=size,
        duration_s=duration,
        width=width,
        height=height,
        has_audio=has_audio,
    )


def duration_s(path: str, tools: FfmpegTools, *, default: Optional[float] = None,
               timeout_s: float = 60.0) -> Optional[float]:
    """Duration in seconds, or ``default`` when it cannot be determined.

    Never raises: callers use this to decide whether a clip is long enough to
    cut, and a probe failure should not fail a publish. The reason is logged.
    """
    try:
        return probe(path, tools, timeout_s=timeout_s).duration_s
    except (MediaError, FfmpegFailed) as exc:
        log.warning("duration probe failed for %s (using default %r): %s",
                    path, default, exc)
        return default


def _int_or_none(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
