"""Binary discovery and one place that actually runs ffmpeg/ffprobe.

Rules that come out of real incidents:

* A missing binary DISABLES the features that need it, with ONE warning at
  startup, rather than failing mid-publish — unless ``media.require_ffmpeg`` is
  set, in which case it is a startup error.
* Every invocation has a timeout. An ffmpeg that hangs on a malformed input
  otherwise wedges a scheduled job forever, and a job that never returns never
  logs.
* Failures raise :class:`~makervox_publish.errors.FfmpegFailed` carrying the tail of
  stderr. Callers that intend to continue anyway swallow it *and log the
  reason*; a silent swallow here is how an encode failure turns into the exact
  upload rejection the encode existed to prevent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional, Sequence

from makervox_publish.errors import FfmpegFailed, FfmpegNotFound
from makervox_publish.logging import get_logger

__all__ = ["FfmpegTools", "find_binary"]

log = get_logger(__name__)

_STDERR_TAIL = 600


def find_binary(configured: str) -> Optional[str]:
    """Resolve a binary name or an absolute path. Returns None when absent."""
    if not configured:
        return None
    expanded = os.path.expanduser(configured)
    if os.path.isabs(expanded):
        return expanded if os.path.isfile(expanded) and os.access(expanded, os.X_OK) else None
    return shutil.which(expanded)


@dataclass(frozen=True)
class FfmpegTools:
    """Resolved ffmpeg/ffprobe paths plus the runner.

    Construct once (usually from config) and pass it down. Discovery happens
    here so that "is ffmpeg installed?" is answered once, at startup, instead of
    once per media operation.
    """

    ffmpeg: Optional[str] = None
    ffprobe: Optional[str] = None

    @classmethod
    def discover(
        cls,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        *,
        require: bool = False,
    ) -> "FfmpegTools":
        ffmpeg = find_binary(ffmpeg_path)
        ffprobe = find_binary(ffprobe_path)
        if require:
            if not ffmpeg:
                raise FfmpegNotFound("ffmpeg", ffmpeg_path)
            if not ffprobe:
                raise FfmpegNotFound("ffprobe", ffprobe_path)
        missing = [n for n, p in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not p]
        if missing:
            log.warning(
                "%s not found on PATH; cover art, duration probing, short cuts "
                "and pre-upload transcodes are DISABLED. Install ffmpeg or set "
                "media.ffmpeg_path / media.ffprobe_path.",
                " and ".join(missing),
            )
        return cls(ffmpeg=ffmpeg, ffprobe=ffprobe)

    @classmethod
    def from_config(cls, media_cfg) -> "FfmpegTools":
        """Build from a :class:`makervox_publish.config.MediaConfig`."""
        return cls.discover(
            getattr(media_cfg, "ffmpeg_path", "ffmpeg"),
            getattr(media_cfg, "ffprobe_path", "ffprobe"),
            require=bool(getattr(media_cfg, "require_ffmpeg", False)),
        )

    # -- availability -------------------------------------------------------- #
    @property
    def available(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)

    def require_ffmpeg(self) -> str:
        if not self.ffmpeg:
            raise FfmpegNotFound("ffmpeg", "ffmpeg")
        return self.ffmpeg

    def require_ffprobe(self) -> str:
        if not self.ffprobe:
            raise FfmpegNotFound("ffprobe", "ffprobe")
        return self.ffprobe

    # -- running ------------------------------------------------------------- #
    def run(
        self,
        args: Sequence[str],
        *,
        timeout_s: float = 120.0,
        binary: str = "ffmpeg",
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run ffmpeg/ffprobe with ``args``, capturing stdout and stderr.

        ``args`` excludes the binary itself and excludes ``-v``/``-nostdin``,
        which are added here so every invocation is quiet and non-interactive.
        An ffmpeg that reads stdin inside a scheduled job blocks forever.
        """
        exe = self.require_ffmpeg() if binary == "ffmpeg" else self.require_ffprobe()
        argv = [exe]
        if binary == "ffmpeg":
            argv += ["-nostdin", "-hide_banner", "-loglevel", "error"]
        else:
            argv += ["-hide_banner", "-loglevel", "error"]
        argv += [str(a) for a in args]

        try:
            proc = subprocess.run(  # noqa: S603 - argv list, no shell
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise FfmpegFailed(argv, None, _tail(exc.stderr), timed_out=True) from exc
        except OSError as exc:
            raise FfmpegFailed(argv, None, str(exc)) from exc

        if check and proc.returncode != 0:
            raise FfmpegFailed(argv, proc.returncode, _tail(proc.stderr))
        return proc


def _tail(stderr) -> str:
    if not stderr:
        return ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    return stderr.strip()[-_STDERR_TAIL:]
