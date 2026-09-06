"""End-to-end publish behaviour, with the network faked out.

What is pinned here, and why each one is a scar rather than a nicety:

* **HTTP 206 is a successful chunk.** A ``status == 200`` check fails every
  multi-chunk upload.
* **An API rejection is never retried.** Re-sending an upload the platform
  refused for spam reasons makes the rejection worse, and each attempt burns
  another pending share against a cap of roughly five.
* **A retry after the init call is a DUPLICATE**, so it does not happen — only a
  transport failure before anything was sent is retried.
* **``auto_post`` never raises.** It is called from a pipeline that must survive
  one dark account.
* **``creator_info`` is not cached**, because a creator can flip to private at
  any moment and a stale privacy option would post against their current
  setting.
* **Branded content plus private visibility is refused locally**, before an
  upload is spent on a combination the platform rejects at the end.
"""

import json
import time

import pytest

from makervox_publish import Config
from makervox_publish.errors import PublishError
from makervox_publish.http import HttpClient, HttpPolicy
from makervox_publish.platforms.tiktok import TikTokClient
from makervox_publish.platforms.tiktok.errors import TikTokApiError

MiB = 1024 * 1024


class FakeResponse:
    def __init__(self, payload=None, status=200, body=None):
        self._payload = payload
        self.status_code = status
        self.text = body if body is not None else json.dumps(payload or {})
        self.content = self.text.encode("utf-8")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class Boom(Exception):
    pass


class FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self.calls = []
        self.responses = list(responses)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP call: {0} {1}".format(method, url))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, FakeResponse) else FakeResponse(item)

    def close(self):
        pass


@pytest.fixture
def reel(tmp_path):
    path = tmp_path / "moonlit_2031-04-09.mp4"
    path.write_bytes(b"\0" * (3 * MiB))
    return str(path)


def build(tmp_path, monkeypatch, responses, **tiktok_overrides):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck-test-000000")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs-test-000000")
    tiktok = {
        "enabled": True,
        "redirect_uri": "http://127.0.0.1:8722/tiktok/callback",
    }
    tiktok.update(tiktok_overrides)
    config = Config.from_mapping({
        "version": 1,
        "state": {"dir": str(tmp_path / "state")},
        "accounts": {"moonlit": {"platforms": ["tiktok"],
                                 "tiktok": {"auto_publish": True}}},
        "platforms": {"tiktok": tiktok},
        "token_stores": {"tiktok_tokens": {
            "impl": "makervox_publish.state.token_store:FileTokenStore",
            "options": {"path": str(tmp_path / "state" / "tiktok_tokens.json")},
        }},
    })
    session = FakeSession(responses)
    http = HttpClient(
        HttpPolicy(timeouts={"default": 5.0, "auth": 5.0, "media_create": 5.0,
                             "upload_chunk": 5.0, "poll": 5.0, "small_write": 5.0},
                   attempts=3, backoff_s=0.0),
        session=session,
    )
    client = TikTokClient.from_config(config, http=http)
    client.auth.store.save({"moonlit": {
        "open_id": "OID-A", "access_token": "AT", "refresh_token": "RT",
        "expires_at": time.time() + 3600, "scope": "user.info.basic,video.upload",
    }})
    client.auth.store.invalidate()
    return client, session


INIT_OK = {"data": {"publish_id": "PID-1", "upload_url": "https://upload.example/x?sig=1"},
           "error": {"code": "ok"}}


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #
def test_inbox_upload_accepts_206_as_success(tmp_path, monkeypatch, reel):
    client, session = build(tmp_path, monkeypatch, [
        INIT_OK,
        FakeResponse(status=206, body=""),      # 206 IS success for a chunk
    ])

    publish_id = client.post_to_inbox("moonlit", reel)

    assert publish_id == "PID-1"
    method, url, kwargs = session.calls[1]
    assert method == "PUT"
    assert kwargs["headers"]["Content-Range"] == "bytes 0-{0}/{1}".format(
        3 * MiB - 1, 3 * MiB
    )
    assert kwargs["headers"]["Content-Length"] == str(3 * MiB)
    assert kwargs["headers"]["Content-Type"] == "video/mp4"


def test_auto_post_returns_the_publish_id(tmp_path, monkeypatch, reel):
    client, _ = build(tmp_path, monkeypatch, [INIT_OK, FakeResponse(status=201, body="")])

    result = client.auto_post("moonlit", reel)

    assert result.ok is True
    assert result.as_tuple() == (True, "PID-1")
    assert result.platform == "tiktok"


# --------------------------------------------------------------------------- #
# rejections
# --------------------------------------------------------------------------- #
def test_spam_rejection_is_reported_once_and_never_retried(tmp_path, monkeypatch, reel):
    """Retrying a spam rejection makes it worse AND burns another pending share."""
    client, session = build(tmp_path, monkeypatch, [
        {"error": {"code": "spam_risk_too_many_pending_share",
                   "message": "too many pending shares", "log_id": "LOG-9"}},
    ])

    result = client.auto_post("moonlit", reel)

    assert result.ok is False
    assert "spam_risk_too_many_pending_share" in result.reason
    assert len(session.calls) == 1, "an API rejection must not be retried"


def test_init_failure_message_carries_the_log_id(tmp_path, monkeypatch, reel):
    client, _ = build(tmp_path, monkeypatch, [
        {"error": {"code": "spam_risk", "message": "no", "log_id": "LOG-42"}},
    ])
    with pytest.raises(TikTokApiError) as excinfo:
        client.post_to_inbox("moonlit", reel)
    assert "LOG-42" in str(excinfo.value), "the only handle support has on a call"


def test_a_rejected_chunk_fails_the_publish(tmp_path, monkeypatch, reel):
    client, _ = build(tmp_path, monkeypatch, [
        INIT_OK,
        FakeResponse(status=403, body="invalid request id"),
    ])
    with pytest.raises(PublishError) as excinfo:
        client.post_to_inbox("moonlit", reel)
    assert "chunk 1/1" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# retry discipline
# --------------------------------------------------------------------------- #
def test_transport_failure_before_init_is_retried(tmp_path, monkeypatch, reel):
    import requests

    client, session = build(tmp_path, monkeypatch, [
        requests.exceptions.ConnectionError("dns"),
        requests.exceptions.ConnectionError("dns"),
        INIT_OK,
        FakeResponse(status=200, body=""),
    ])

    result = client.auto_post("moonlit", reel)

    assert result.ok is True
    assert len(session.calls) == 4


def test_transport_failure_AFTER_init_is_not_retried(tmp_path, monkeypatch, reel):
    """A retry past this point mints a SECOND pending share for one reel."""
    import requests

    client, session = build(tmp_path, monkeypatch, [
        INIT_OK,
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.ConnectionError("reset"),
    ])

    result = client.auto_post("moonlit", reel)

    assert result.ok is False
    # The chunk PUT itself was retried by the HTTP policy (safe: same upload
    # session, same publish id) but the whole publish was NOT re-initialised.
    inits = [c for c in session.calls if c[1].endswith("/inbox/video/init/")]
    assert len(inits) == 1, "a second init would be a second pending share"


def test_auto_post_never_raises_on_a_missing_file(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch, [])
    result = client.auto_post("moonlit", "/no/such/reel.mp4")
    assert result.ok is False
    assert "FileNotFoundError" in result.reason


def test_auto_post_skips_an_account_with_auto_publish_off(tmp_path, monkeypatch, reel):
    client, session = build(
        tmp_path, monkeypatch, [],
        publish={"auto_publish_disabled_accounts": ["moonlit"]},
    )
    result = client.auto_post("moonlit", reel)
    assert result.ok is False
    assert "pending-share cap" in result.reason
    assert session.calls == [], "nothing is uploaded for a disabled account"


# --------------------------------------------------------------------------- #
# direct post guards
# --------------------------------------------------------------------------- #
CREATOR_OK = {"data": {"creator_nickname": "Moonlit",
                       "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
                       "max_video_post_duration_sec": 600},
              "error": {"code": "ok"}}


def test_creator_info_is_not_cached(tmp_path, monkeypatch):
    client, session = build(tmp_path, monkeypatch, [CREATOR_OK, CREATOR_OK])

    client.creator_info("moonlit")
    client.creator_info("moonlit")

    assert len(session.calls) == 2, (
        "a creator can flip to private between two posts; a cached "
        "PUBLIC_TO_EVERYONE would post against their current setting"
    )


def test_direct_post_refuses_a_privacy_level_the_creator_does_not_offer(
        tmp_path, monkeypatch, reel):
    client, _ = build(tmp_path, monkeypatch, [CREATOR_OK])
    with pytest.raises(PublishError) as excinfo:
        client.direct_post("moonlit", reel, "caption",
                           privacy_level="MUTUAL_FOLLOW_FRIENDS")
    assert "PUBLIC_TO_EVERYONE" in str(excinfo.value)


def test_direct_post_requires_an_explicit_privacy_level(tmp_path, monkeypatch, reel):
    """The API forbids a pre-selected default, so there is no fallback here."""
    client, _ = build(tmp_path, monkeypatch, [CREATOR_OK])
    with pytest.raises(PublishError) as excinfo:
        client.direct_post("moonlit", reel, "caption")
    assert "chosen by a person" in str(excinfo.value)


def test_branded_private_is_refused_before_the_upload(tmp_path, monkeypatch, reel):
    client, session = build(tmp_path, monkeypatch, [CREATOR_OK])
    with pytest.raises(PublishError) as excinfo:
        client.direct_post("moonlit", reel, "caption", privacy_level="SELF_ONLY",
                           brand_content_toggle=True)
    assert "branded content" in str(excinfo.value).lower()
    # Only the creator_info probe happened: no upload was spent on a
    # combination the platform rejects at the very end.
    assert len(session.calls) == 1


def test_direct_post_truncates_the_caption_to_the_platform_limit(
        tmp_path, monkeypatch, reel):
    client, session = build(tmp_path, monkeypatch, [
        CREATOR_OK, INIT_OK, FakeResponse(status=200, body=""),
    ])
    client.direct_post("moonlit", reel, "x" * 5000,
                       privacy_level="PUBLIC_TO_EVERYONE")
    init_call = [c for c in session.calls if c[1].endswith("/publish/video/init/")][0]
    assert len(init_call[2]["json"]["post_info"]["title"]) == 2200


# --------------------------------------------------------------------------- #
# confirmation
# --------------------------------------------------------------------------- #
def test_confirm_reports_the_inbox_landing(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch, [
        {"data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"}},
        {"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}},
    ])
    ok, status = client.confirm("moonlit", "PID-1", attempts=4, delay_s=0)
    assert (ok, status) == (True, "SEND_TO_USER_INBOX")


def test_confirm_reports_a_failure(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch, [
        {"data": {"status": "FAILED"}, "error": {"code": "ok"}},
    ])
    ok, status = client.confirm("moonlit", "PID-1", attempts=4, delay_s=0)
    assert (ok, status) == (False, "FAILED")


def test_still_processing_counts_as_accepted(tmp_path, monkeypatch):
    """The bytes are in the platform's hands; a "failed" here sends someone
    chasing a post that is about to appear."""
    client, _ = build(tmp_path, monkeypatch, [
        {"data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"}}
        for _ in range(3)
    ])
    ok, status = client.confirm("moonlit", "PID-1", attempts=3, delay_s=0)
    assert ok is True
    assert status.startswith("PROCESSING")
