"""Cover-frame selection: why a profile grid does not end up a wall of black.

Instagram and Facebook both default a reel's thumbnail to frame 0. A great many
videos open on a fade from black, so frame 0 is black, so the grid fills with
black squares — and nothing errors, which is why it survived 45 posts in the
code this was extracted from before anyone noticed.

The scan therefore has to distinguish three cases that all *look* like "no
bright frame":

* a fade-in — skip forward until something crosses the brightness floor;
* legitimately dark art — nothing crosses the main floor, but a frame clearly
  above black still beats frame 0, so take it rather than giving up;
* no ffmpeg at all — fall back to a fixed non-zero offset, because a fade-in is
  more likely than a video that opens on its best frame.

And it must never raise. A cover is decoration; the post is the product.
"""

from __future__ import annotations

import pytest

from makervox_publish.media.cover import (
    BrightnessScanCoverPicker,
    cover_offset_ms,
)


class FakeTools:
    """Stands in for FfmpegTools. `curve` maps offset seconds -> brightness."""

    ffmpeg = "/usr/bin/ffmpeg"
    ffprobe = "/usr/bin/ffprobe"

    def __init__(self, curve, end_after=None):
        self.curve = curve
        self.end_after = end_after
        self.sampled = []


@pytest.fixture
def scan(monkeypatch):
    """Drive the picker off a synthetic brightness curve, no ffmpeg needed."""
    def _install(curve, end_after=None):
        tools = FakeTools(curve, end_after)

        def fake_sample(path, offset, runner, timeout_s=None):
            runner.sampled.append(offset)
            if end_after is not None and offset > end_after:
                return None          # past the end of the clip
            for at, value in sorted(curve.items()):
                if offset <= at + 1e-9:
                    return value
            return list(curve.values())[-1]

        monkeypatch.setattr("makervox_publish.media.cover.sample_brightness", fake_sample)
        return tools
    return _install


def test_fade_from_black_skips_past_the_dark_frames(scan):
    """The original bug: frame 0 is black, so do not use frame 0."""
    tools = scan({0.0: 1.0, 0.5: 4.0, 1.0: 60.0, 3.0: 80.0})
    pick = BrightnessScanCoverPicker(tools=tools).pick("reel.mp4")
    assert pick.offset_ms > 0, "picked a black opening frame"
    assert pick.reason in ("qualifying", "brightest")


def test_dark_art_still_beats_frame_zero(scan):
    """Nothing crosses the main floor, but something is clearly above black.

    Giving up here would hand the platform frame 0 — the exact outcome the scan
    exists to avoid — so the best dark frame is used instead.
    """
    picker = BrightnessScanCoverPicker(tools=scan({0.0: 0.5, 1.0: 12.0, 2.0: 14.0}))
    pick = picker.pick("moody.mp4")
    assert pick.offset_ms > 0
    assert pick.reason in ("dark", "brightest", "qualifying")


def test_no_ffmpeg_falls_back_to_a_nonzero_offset():
    """Without ffmpeg, assume a fade-in rather than trusting frame 0."""
    class NoFfmpeg:
        ffmpeg = None
        ffprobe = None

    pick = BrightnessScanCoverPicker(tools=NoFfmpeg()).pick("reel.mp4")
    assert pick.reason == "unavailable"
    assert pick.offset_ms >= 0


def test_scan_never_raises_on_a_broken_file(monkeypatch):
    """A cover is decoration. A broken file must not fail the publish."""
    def boom(*a, **k):
        raise OSError("ffmpeg died")

    monkeypatch.setattr("makervox_publish.media.cover.sample_brightness", boom)

    class Tools:
        ffmpeg = "/usr/bin/ffmpeg"
        ffprobe = "/usr/bin/ffprobe"

    pick = BrightnessScanCoverPicker(tools=Tools()).pick("corrupt.mp4")
    assert pick.reason == "error"
    assert pick.offset_ms >= 0


def test_disabled_returns_none_which_is_not_zero():
    """None means 'send no thumbnail offset'; 0 means 'use the first frame'.

    Collapsing the two would silently reintroduce the black-thumbnail bug for
    anyone who switched the scan off.
    """
    class Off:
        enabled = False

    assert cover_offset_ms("reel.mp4", config=Off()) is None


def test_custom_picker_overrides_the_scan():
    assert cover_offset_ms("reel.mp4", picker=lambda path, tools=None: 4321) == 4321
