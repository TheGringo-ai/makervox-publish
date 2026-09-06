"""A short, hook-forward cut of a clip, for one platform only.

WHY IT EXISTS
-------------
Shorter clip -> higher completion rate -> more reach on a cold account. The cut
is posted to ONE platform while the full clip goes everywhere else, so it is
made into a NEW temp file and the master is never touched.

TWO THINGS THAT LOOK LIKE DETAILS AND ARE NOT
---------------------------------------------
* A clip that is already short enough is returned UNCHANGED. Re-encoding a
  15-second clip to make a 15-second clip costs a generation of quality for
  nothing.
* The temp path belongs to the caller, who deletes it — but the DELIVERY LEDGER
  must record the SOURCE file, not this cut. A ledger row naming a temp file
  (which has no date in its name) reads to a recovery job as "never delivered",
  and it re-posts the whole day. That is why every publish path here carries
  ``source_path`` separately from the file that goes over the wire.

Best effort by design: any failure returns the original path. It never breaks a
publish, and unlike the transcode (whose failure would hand back an oversized
file and reproduce the exact upload rejection it exists to prevent) falling back
to the full clip here is harmless — it just posts the long version.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from typing import Iterator, Optional, Sequence

from makervox_publish.logging import get_logger, swallowed
from makervox_publish.media.ffmpeg import FfmpegTools
from makervox_publish.media.probe import duration_s

__all__ = ["ShortCutResult", "short_cut", "short_cut_for_publish"]

log = get_logger(__name__)

DEFAULT_ENCODER_ARGS = (
    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-movflags", "+faststart",
)


@dataclass(frozen=True)
class ShortCutResult:
    """``path`` is what to upload; ``is_temp`` says whether to delete it."""

    path: str
    is_temp: bool
    source_path: str
    reason: str = ""

    def cleanup(self) -> None:
        if not self.is_temp:
            return
        try:
            os.unlink(self.path)
        except OSError as exc:  # pragma: no cover - best effort
            log.debug("could not remove temp cut %s: %s", self.path, exc)


def short_cut(
    src: str,
    tools: FfmpegTools,
    *,
    seconds: float = 15.0,
    fade_s: float = 0.6,
    skip_if_duration_under_s: Optional[float] = None,
    encoder_args: Sequence[str] = DEFAULT_ENCODER_ARGS,
    timeout_s: float = 300.0,
) -> ShortCutResult:
    """Trim ``src`` to its first ``seconds`` with a soft fade out.

    Returns the ORIGINAL path (``is_temp=False``) when the clip is already short
    enough, when ffmpeg is unavailable, or when anything fails.
    """
    source = os.path.abspath(os.path.expanduser(str(src)))
    threshold = (
        float(seconds) + 2.0 if skip_if_duration_under_s is None
        else float(skip_if_duration_under_s)
    )

    if not tools or not tools.ffmpeg or not tools.ffprobe:
        log.warning("short cut skipped for %s: ffmpeg/ffprobe unavailable; "
                    "posting the full clip", os.path.basename(source))
        return ShortCutResult(source, False, source, "ffmpeg unavailable")

    duration = duration_s(source, tools, default=0.0)
    if not duration or duration <= 0:
        return ShortCutResult(source, False, source, "duration unknown")
    if duration <= threshold:
        # Already short enough. Posting the original avoids a pointless
        # re-encode AND avoids a fade on a clip that has no room for one.
        return ShortCutResult(source, False, source, "already short enough")

    fd, out = tempfile.mkstemp(suffix="_cut.mp4")
    os.close(fd)
    fade = max(0.0, float(fade_s))
    fade_start = max(0.0, float(seconds) - fade)
    args = ["-y", "-i", source, "-t", "{0:g}".format(seconds)]
    if fade > 0:
        args += [
            "-vf", "fade=t=out:st={0:.2f}:d={1:g}".format(fade_start, fade),
            "-af", "afade=t=out:st={0:.2f}:d={1:g}".format(fade_start, fade),
        ]
    args += list(encoder_args) + [out]

    try:
        tools.run(args, timeout_s=timeout_s, binary="ffmpeg")
    except Exception as exc:
        # Logged, not swallowed silently: a cut that quietly stopped happening
        # looks like a reach regression with no cause.
        swallowed(log, "short cut of {0}".format(os.path.basename(source)), exc,
                  detail="posting the full clip instead")
        try:
            os.unlink(out)
        except OSError:
            pass
        return ShortCutResult(source, False, source, "encode failed")

    if not (os.path.exists(out) and os.path.getsize(out)):
        try:
            os.unlink(out)
        except OSError:
            pass
        return ShortCutResult(source, False, source, "empty output")

    return ShortCutResult(out, True, source, "cut to {0:g}s".format(seconds))


@contextlib.contextmanager
def short_cut_for_publish(src: str, tools: FfmpegTools, cfg=None) -> Iterator[ShortCutResult]:
    """Context manager form: yields the cut and always cleans it up.

    ``cfg`` is a :class:`makervox_publish.config.ShortCutConfig`; ``enabled = false``
    yields the original path untouched.
    """
    source = os.path.abspath(os.path.expanduser(str(src)))
    if cfg is not None and not getattr(cfg, "enabled", True):
        yield ShortCutResult(source, False, source, "disabled")
        return
    result = short_cut(
        source,
        tools,
        seconds=float(getattr(cfg, "seconds", 15.0)),
        fade_s=float(getattr(cfg, "fade_s", 0.6)),
        skip_if_duration_under_s=getattr(cfg, "skip_if_duration_under_s", None),
        encoder_args=tuple(getattr(cfg, "encoder_args", DEFAULT_ENCODER_ARGS)),
        timeout_s=float(getattr(cfg, "timeout_s", 300.0)),
    )
    try:
        yield result
    finally:
        result.cleanup()
