"""cover_offset_ms() — THE shared home for cover-frame selection.

WHY THIS LIVES HERE, AND NOT UNDER A PLATFORM
---------------------------------------------
Both the Facebook and the Instagram publisher need a cover offset, and in the
original code the function lived inside one of them. The other imported it, the
first imported the other back for Page credentials, and the two modules were
locked in a circular import that lazy imports only hid.

The fix is layering, not lazy imports: this function is pure ffmpeg brightness
scanning with zero platform code in it, so it moves DOWN a layer and both
publishers import it from here. Neither owns it, and the cycle cannot come back.

WHAT IT DOES
------------
Reels that fade in from black start at brightness 0.0 — frame 0 is literally
black. A platform that auto-picks "frame 0" as the preferred thumbnail renders
a whole profile grid as black tiles. So: sample brightness forward through the
first few seconds and return the first frame that is past the fade.

Two floors, on purpose:

* ``brightness_floor`` — "past the fade, real content".
* ``fallback_floor``  — legitimately dark art never crosses the first floor,
  and for that footage anything clearly above black still beats frame 0.

``take_first_qualifying`` is the default because the cover should match the
opening beat of the clip; the brightest frame in the first six seconds is often
a flash halfway through and reads as a different video.

Nothing here raises. A cover frame is never worth failing a post over — but a
cover frame that silently vanished is how you end up with a wall of black
thumbnails and no idea why, so every failure is logged with its reason.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from postvox.errors import FfmpegFailed, MediaError
from postvox.logging import get_logger, swallowed
from postvox.media.ffmpeg import FfmpegTools

__all__ = [
    "BrightnessSample",
    "CoverPick",
    "BrightnessScanCoverPicker",
    "cover_offset_ms",
    "sample_brightness",
]

log = get_logger(__name__)

#: Frames are downscaled to this square before averaging. Small enough that the
#: decode dominates, large enough that a single bright pixel cannot swing it.
_SAMPLE_EDGE = 32


@dataclass(frozen=True)
class BrightnessSample:
    """One measured frame. ``brightness`` is a 0-255 mean over a grayscale frame."""

    offset_s: float
    brightness: float


@dataclass(frozen=True)
class CoverPick:
    """The chosen offset plus enough detail to explain it in a log line.

    Callers that just want the number use :func:`cover_offset_ms`; this exists
    so ``postvox doctor`` and the tests can assert on the REASON, not only on
    the final integer.
    """

    offset_ms: int
    brightness: Optional[float] = None
    #: one of: qualifying | fallback_dark | brightest | floor_default |
    #: unreadable | unavailable | error
    reason: str = "floor_default"
    samples: Tuple[BrightnessSample, ...] = ()

    @property
    def offset_s(self) -> float:
        return self.offset_ms / 1000.0

    @property
    def measured(self) -> bool:
        """True when a real frame was measured, rather than a default applied."""
        return self.reason in ("qualifying", "fallback_dark", "brightest")


def sample_brightness(
    path: str,
    offset_s: float,
    tools: FfmpegTools,
    *,
    timeout_s: float = 30.0,
) -> Optional[float]:
    """Mean luma (0-255) of the frame at ``offset_s``, or None if unreadable.

    ``-ss`` before ``-i`` is an input seek: it jumps rather than decoding
    everything up to that point, which is what keeps a two-dozen-sample scan
    cheap.
    """
    try:
        proc = tools.run(
            [
                "-ss", "{0:.3f}".format(max(0.0, offset_s)),
                "-i", path,
                "-frames:v", "1",
                "-vf", "scale={0}:{0}".format(_SAMPLE_EDGE),
                "-pix_fmt", "gray",
                "-f", "rawvideo",
                "-",
            ],
            timeout_s=timeout_s,
            binary="ffmpeg",
        )
    except (FfmpegFailed, MediaError) as exc:
        log.debug("brightness sample at %.2fs failed: %s", offset_s, exc)
        return None

    raw = proc.stdout or b""
    if not raw:
        return None
    return float(sum(raw)) / float(len(raw))


@dataclass
class BrightnessScanCoverPicker:
    """Scan forward through the opening seconds and pick the first real frame.

    Defaults are calibrated on reels that fade in from black. On footage that
    opens bright they are harmless but not meaningful — tune them, or set
    ``media.cover.enabled = false`` and let the platform choose.

    Constructed from ``[media.cover]`` via its ``impl``/``options`` keys, so the
    options below are exactly the config keys.
    """

    scan_until_s: float = 6.0
    step_s: float = 0.25
    brightness_floor: float = 10.0
    min_offset_s: float = 0.75
    fallback_floor: float = 5.0
    take_first_qualifying: bool = True
    tools: Optional[FfmpegTools] = None
    sample_timeout_s: float = 30.0
    _samples: List[BrightnessSample] = field(default_factory=list, repr=False)

    # -- construction -------------------------------------------------------- #
    @classmethod
    def from_config(cls, cover_cfg, tools: Optional[FfmpegTools] = None
                    ) -> "BrightnessScanCoverPicker":
        """Build from a :class:`postvox.config.CoverConfig`."""
        return cls(
            scan_until_s=float(getattr(cover_cfg, "scan_until_s", 6.0)),
            step_s=float(getattr(cover_cfg, "step_s", 0.25)),
            brightness_floor=float(getattr(cover_cfg, "brightness_floor", 10.0)),
            min_offset_s=float(getattr(cover_cfg, "min_offset_s", 0.75)),
            fallback_floor=float(getattr(cover_cfg, "fallback_floor", 5.0)),
            take_first_qualifying=bool(getattr(cover_cfg, "take_first_qualifying", True)),
            tools=tools,
        )

    # -- the scan ------------------------------------------------------------ #
    def offsets(self) -> List[float]:
        """The sample points, from 0 upward.

        Sampling starts at 0 even though nothing before ``min_offset_s`` can be
        chosen: the frame-0 measurement is what tells you (and the log) that
        this clip fades in from black at all.
        """
        out = []  # type: List[float]
        step = max(0.001, float(self.step_s))
        offset = 0.0
        while offset <= float(self.scan_until_s) + 1e-9:
            out.append(round(offset, 3))
            offset += step
        return out

    def pick(self, path: str, tools: Optional[FfmpegTools] = None) -> CoverPick:
        """Choose a cover offset for ``path``. Never raises."""
        floor_default_ms = int(round(max(0.0, self.min_offset_s) * 1000))
        runner = tools or self.tools
        if runner is None or not runner.ffmpeg:
            log.warning(
                "cover scan skipped for %s: ffmpeg is unavailable; falling back "
                "to a fixed %dms offset so a fade-in does not become a black "
                "thumbnail", os.path.basename(str(path)), floor_default_ms,
            )
            return CoverPick(floor_default_ms, reason="unavailable")

        samples = []  # type: List[BrightnessSample]
        best = None  # type: Optional[BrightnessSample]
        best_dark = None  # type: Optional[BrightnessSample]

        try:
            for offset in self.offsets():
                brightness = sample_brightness(
                    str(path), offset, runner, timeout_s=self.sample_timeout_s
                )
                if brightness is None:
                    # Past the end of the clip, or an unreadable frame. Either
                    # way there is nothing further forward to find.
                    break
                sample = BrightnessSample(offset, brightness)
                samples.append(sample)

                if offset + 1e-9 < self.min_offset_s:
                    continue

                if brightness >= self.brightness_floor:
                    if self.take_first_qualifying:
                        return CoverPick(
                            int(round(offset * 1000)), brightness, "qualifying",
                            tuple(samples),
                        )
                    if best is None or brightness > best.brightness:
                        best = sample
                elif brightness >= self.fallback_floor and best_dark is None:
                    best_dark = sample
        except Exception as exc:  # a broken file, a killed subprocess, a full disk
            swallowed(log, "cover scan for {0}".format(path), exc)
            return CoverPick(floor_default_ms, reason="error", samples=tuple(samples))

        self._samples = samples

        if best is not None:
            return CoverPick(int(round(best.offset_s * 1000)), best.brightness,
                             "brightest", tuple(samples))
        if best_dark is not None:
            # Legitimately dark art: nothing crosses the main floor, but this
            # frame is clearly above black and beats the platform's frame 0.
            log.info(
                "cover for %s is dark art: best frame %.2fs at brightness %.1f "
                "(below floor %.1f, above fallback %.1f)",
                os.path.basename(str(path)), best_dark.offset_s, best_dark.brightness,
                self.brightness_floor, self.fallback_floor,
            )
            return CoverPick(int(round(best_dark.offset_s * 1000)), best_dark.brightness,
                             "fallback_dark", tuple(samples))

        if not samples:
            # Not "everything was black" — nothing was readable at all. Saying
            # so is the difference between "tune your floors" and "your file is
            # missing or corrupt".
            log.warning(
                "could not read any frame from %s (missing, corrupt, or an "
                "unsupported codec); using a fixed %dms cover offset",
                path, floor_default_ms,
            )
            return CoverPick(floor_default_ms, reason="unreadable")

        log.warning(
            "no frame in the first %.1fs of %s cleared either brightness floor "
            "(%.1f / %.1f); using a fixed %dms offset",
            self.scan_until_s, os.path.basename(str(path)),
            self.brightness_floor, self.fallback_floor, floor_default_ms,
        )
        return CoverPick(floor_default_ms, reason="floor_default", samples=tuple(samples))

    # -- callable, so any `impl` that returns an int can replace it ----------- #
    def __call__(self, path: str, tools: Optional[FfmpegTools] = None) -> int:
        return self.pick(path, tools).offset_ms


def cover_offset_ms(
    path: str,
    *,
    tools: Optional[FfmpegTools] = None,
    config=None,
    picker=None,
) -> Optional[int]:
    """Milliseconds into ``path`` that should be used as the cover frame.

    This is the function both Meta publishers call. Returns None only when cover
    selection is switched off in config, which means "do not send a thumbnail
    offset at all" — distinct from 0, which means "the very first frame".

    ``config`` is a :class:`postvox.config.CoverConfig`; ``picker`` overrides it
    with any callable taking ``(path)`` and returning milliseconds, which is how
    a custom ``media.cover.impl`` is injected.
    """
    if config is not None and not getattr(config, "enabled", True):
        return None

    if picker is None:
        if config is not None:
            picker = BrightnessScanCoverPicker.from_config(config, tools)
        else:
            picker = BrightnessScanCoverPicker(tools=tools)

    try:
        if isinstance(picker, BrightnessScanCoverPicker):
            return picker.pick(path, tools).offset_ms
        result = picker(path)
        if result is None:
            return None
        return int(result)
    except Exception as exc:  # a third-party picker misbehaving
        swallowed(log, "cover picker {0!r}".format(type(picker).__name__), exc)
        min_offset = float(getattr(config, "min_offset_s", 0.75)) if config else 0.75
        return int(round(min_offset * 1000))
