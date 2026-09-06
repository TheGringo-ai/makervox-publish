"""Instagram's defining constraint: it PULLS media from a URL.

There is no byte upload. Instagram fetches the file itself, which produces two
failures that look nothing like each other but share a cause:

* a local file has to be staged somewhere publicly reachable first, and stay
  reachable for the whole ingest;
* the URL has to be one Instagram will actually fetch. Facebook-hosted source
  URLs are refused, which is why the staging layer exists at all rather than
  reusing whatever the Facebook publisher already uploaded.

The third property here is smaller and bit harder: media type is sniffed from
the URL, and signed URLs carry query strings. `clip.mp4?X-Goog-Signature=...`
must not be read as a photo.
"""

from __future__ import annotations

import pytest

from makervox_publish.platforms.meta.instagram import InstagramPublisher


class _Publish:
    #: Mirrors InstagramPublishConfig's real default. Kept explicit so a change
    #: to the shipped default shows up here as a decision, not a silent drift.
    media_type_from_extension = (".mp4", ".mov")
    send_thumb_offset = True


class _Cfg:
    publish = _Publish()


def _pub():
    """A publisher with only the surface these paths touch.

    Built with __new__ deliberately: from_config wants a whole Config, a
    credential chain and an HTTP client, none of which say anything about URL
    parsing. Constructing all of that would test the fixture, not the code.
    """
    pub = InstagramPublisher.__new__(InstagramPublisher)
    pub.cfg = _Cfg()
    pub.platform = "instagram"
    return pub


# ---------------------------------------------------------- media type sniff #

@pytest.mark.parametrize("url,is_video", [
    ("https://example.com/clip.mp4", True),
    ("https://example.com/clip.mov", True),
    ("https://example.com/still.jpg", False),
    ("https://example.com/still.png", False),
    # the one that actually broke: a signed URL's query string
    ("https://storage.googleapis.com/b/clip.mp4?X-Goog-Signature=deadbeef", True),
    ("https://storage.googleapis.com/b/still.jpg?X-Goog-Expires=3600", False),
])
def test_media_type_is_sniffed_with_the_query_string_stripped(url, is_video):
    assert _pub()._is_video(url) is is_video


def test_a_signed_video_url_is_not_mistaken_for_a_photo():
    """Explicit regression: leaving the query on makes every signed mp4 a photo.

    Posting a video through the image_url parameter fails in a way that reads
    like a bad file rather than a bad URL parse.
    """
    signed = ("https://storage.googleapis.com/bucket/reel.mp4"
              "?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Signature=abc123")
    assert _pub()._is_video(signed) is True


# -------------------------------------------------------------- guard rails #

def test_missing_media_fails_without_raising():
    """Every publish path returns a result; a batch must survive one bad account."""
    pub = _pub()
    result = pub.post("caption", "", creds={}, account="acct")
    assert result.ok is False
    assert "image or a video" in result.reason


def test_a_local_path_that_does_not_exist_is_a_clean_failure(monkeypatch):
    pub = _pub()

    class Spec:
        def not_configured_reason(self, missing):
            return "missing: {0}".format(missing)

    pub.spec = Spec()
    monkeypatch.setattr(
        InstagramPublisher, "direct_credentials",
        lambda self, creds: ("ig-user", "token", None),
    )
    result = pub.post("caption", "/nope/missing.mp4", creds={}, account="acct")
    assert result.ok is False
    assert "hosted URL or an existing local file" in result.reason


def test_unconfigured_credentials_fail_without_touching_the_network(monkeypatch):
    pub = _pub()

    class Spec:
        def not_configured_reason(self, missing):
            return "not configured: {0}".format(", ".join(missing))

    pub.spec = Spec()
    monkeypatch.setattr(
        InstagramPublisher, "direct_credentials",
        lambda self, creds: (None, None, ["IG_USER_ID"]),
    )
    result = pub.post("caption", "https://example.com/clip.mp4", creds={},
                      account="acct")
    assert result.ok is False
    assert "IG_USER_ID" in result.reason


def test_an_already_hosted_url_skips_staging(monkeypatch):
    """If the caller solved pull-by-URL themselves, do not re-upload their file.

    Pinned because staging a URL that is already public is both pointless and a
    second place for the ingest to fail.
    """
    pub = _pub()
    seen = {}
    # instagram_base is a read-only property derived from config
    monkeypatch.setattr(InstagramPublisher, "instagram_base",
                        property(lambda self: "https://graph.instagram.com"))

    monkeypatch.setattr(
        InstagramPublisher, "direct_credentials",
        lambda self, creds: ("ig-user", "token", None),
    )

    def fake_publish(self, ig_user, token, caption, url, **kw):
        seen.update(url=url, path_used=kw.get("path_used"),
                    thumb=kw.get("thumb_ms"))
        return "published"

    monkeypatch.setattr(InstagramPublisher, "_publish", fake_publish)

    hosted = "https://cdn.example.com/reel.mp4"
    assert pub.post("caption", hosted, creds={}, account="acct") == "published"
    assert seen["url"] == hosted
    assert seen["path_used"] == "hosted_url"
    assert seen["thumb"] is None, "a hosted URL carries no local file to scan"
