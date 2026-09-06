"""Pre-upload downscale, for platforms with a practical byte ceiling.

THE SCAR
--------
Some platforms reject an oversized video LATE and unhelpfully: a large reel
(measured: ~110 MB for 44 seconds) fails the upload with an "invalid request id"
403, which reads like an auth or session problem and is really a size problem.
Re-encoding anything over the platform's practical ceiling to a
streaming-friendly ~9 Mbps H.264 makes it land reliably.

THE SECOND SCAR, WHICH IS THE REASON THIS IS ITS OWN MODULE
-----------------------------------------------------------
The original caught every exception here and handed back the ORIGINAL,
oversized path — silently. So when ffmpeg failed (or was not installed), the
upload then failed with exactly the 403 the transcode existed to prevent, and
nothing in the logs connected the two. ``on_failure`` therefore defaults to
``raise``. ``use_original`` is available for anyone who genuinely prefers a
likely-doomed attempt over an abort, and even that logs at ERROR.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass, field
from typing import Iterator, Optional, Tuple

from postvox.errors import MediaError
from postvox.logging import get_logger
from postvox.media.ffmpeg import FfmpegTools

__all__ = ["TranscodeSettings", "SizedMedia", "sized_for_upload", "transcode"]

log = get_logger(__name__)

_DEFAULT_ENCODER_ARGS = (
    "-c:v", "libx264", "-crf", "24", "-maxrate", "9M", "-bufsize", "18M",
    "-preset", "veryfast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
)


@dataclass(frozen=True)
class TranscodeSettings:
    enabled: bool = True
    encoder_args: Tuple[str, ...] = _DEFAULT_ENCODER_ARGS
    timeout_s: float = 900.0
    #: raise | use_original
    on_failure: str = "raise"

    @classmethod
    def from_config(cls, transcode_cfg) -> "TranscodeSettings":
        """Build from a :class:`postvox.config.TranscodeConfig`."""
        if transcode_cfg is None:
            return cls()
        args = tuple(getattr(transcode_cfg, "encoder_args", ()) or _DEFAULT_ENCODER_ARGS)
        return cls(
            enabled=bool(getattr(transcode_cfg, "enabled", True)),
            encoder_args=args,
            timeout_s=float(getattr(transcode_cfg, "timeout_s", 900.0)),
            on_failure=str(getattr(transcode_cfg, "on_failure", "raise")),
        )


@dataclass(frozen=True)
class SizedMedia:
    """What to upload, and what it came from.

    ``source_path`` is what the LEDGER and the content fingerprint must use. The
    temp file is a wire detail: one code path may send a re-encode while a
    recovery path sends the master, and fingerprinting the wire bytes would
    never match across the two.
    """

    path: str
    source_path: str
    source_bytes: int
    final_bytes: int
    transcoded: bool = False
    temp_path: Optional[str] = None
    extra: dict = field(default_factory=dict)


def transcode(
    source: str,
    destination: str,
    *,
    tools: FfmpegTools,
    settings: Optional[TranscodeSettings] = None,
) -> str:
    """Re-encode ``source`` to ``destination``. Raises :class:`MediaError` on failure."""
    settings = settings or TranscodeSettings()
    args = ["-y", "-i", source] + [str(a) for a in settings.encoder_args] + [destination]
    tools.run(args, timeout_s=settings.timeout_s, binary="ffmpeg")
    if not os.path.exists(destination) or os.path.getsize(destination) == 0:
        raise MediaError(
            "transcode of {0} produced no output — the source may be unreadable "
            "or the encoder arguments may be wrong".format(source)
        )
    return destination


@contextlib.contextmanager
def sized_for_upload(
    path: str,
    max_bytes: int,
    *,
    tools: FfmpegTools,
    settings: Optional[TranscodeSettings] = None,
) -> Iterator[SizedMedia]:
    """Yield a file that is under ``max_bytes``, cleaning up any temp copy.

    Under the ceiling, or with transcoding disabled, this is a pass-through and
    creates nothing. Over it, the re-encode goes to a temp file that is removed
    when the block exits — including when the publish inside it raises.
    """
    settings = settings or TranscodeSettings()
    full = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(full):
        raise MediaError("no such media file: {0}".format(full))
    size = os.path.getsize(full)

    if size <= max_bytes or not settings.enabled:
        yield SizedMedia(path=full, source_path=full, source_bytes=size, final_bytes=size)
        return

    tmp = None
    try:
        handle = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        handle.close()
        tmp = handle.name
        transcode(full, tmp, tools=tools, settings=settings)
        new_size = os.path.getsize(tmp)
        if new_size >= size:
            # A re-encode that got BIGGER has not solved the problem it was for.
            # Treat it as a failure rather than uploading either file blindly.
            raise MediaError(
                "transcode of {0} grew from {1} to {2} bytes; it is still over "
                "the {3}-byte ceiling".format(full, size, new_size, max_bytes)
            )
        if new_size > max_bytes:
            raise MediaError(
                "transcode of {0} reached {1} bytes, still over the {2}-byte "
                "ceiling — lower media.transcode maxrate/crf for this "
                "source".format(full, new_size, max_bytes)
            )
        log.info("transcoded %s for upload: %d MB -> %d MB",
                 os.path.basename(full), size // 1048576, new_size // 1048576)
        yield SizedMedia(path=tmp, source_path=full, source_bytes=size,
                         final_bytes=new_size, transcoded=True, temp_path=tmp)
        return
    except MediaError as exc:
        if settings.on_failure == "use_original":
            # Logged at ERROR, never swallowed: the upload that follows is
            # likely to be rejected for exactly the size this was meant to fix.
            log.error(
                "transcode failed for %s (%s); uploading the ORIGINAL %d-byte "
                "file, which the platform is likely to reject for size. Set "
                "media.transcode.on_failure = 'raise' to stop instead.",
                full, exc, size,
            )
            if tmp and os.path.exists(tmp):
                _unlink(tmp)
                tmp = None
            yield SizedMedia(path=full, source_path=full, source_bytes=size,
                             final_bytes=size)
            return
        raise
    finally:
        if tmp and os.path.exists(tmp):
            _unlink(tmp)


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError as exc:  # pragma: no cover - best effort
        log.warning("could not remove temporary media file %s: %s", path, exc)
