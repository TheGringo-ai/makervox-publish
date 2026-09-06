"""makervox_publish.media — external binaries and the media helpers every platform shares.

``cover_offset_ms`` lives here rather than inside a platform module on purpose:
both Meta publishers need it, and putting it in either one of them is what
created the circular import between Facebook and Instagram in the code this
package was extracted from.
"""

from __future__ import annotations

from makervox_publish.media.cover import (
    BrightnessSample,
    BrightnessScanCoverPicker,
    CoverPick,
    cover_offset_ms,
    sample_brightness,
)
from makervox_publish.media.ffmpeg import FfmpegTools, find_binary
from makervox_publish.media.probe import MediaInfo, duration_s, probe

__all__ = [
    "FfmpegTools",
    "find_binary",
    "MediaInfo",
    "probe",
    "duration_s",
    "cover_offset_ms",
    "BrightnessScanCoverPicker",
    "BrightnessSample",
    "CoverPick",
    "sample_brightness",
]
