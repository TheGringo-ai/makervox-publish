"""The publish flow: what happens, in what order, and what must not block it.

Three properties are pinned here because each one cost a real post or a real
bill in the code this was extracted from:

* the counter is written BEFORE the optional link reply, so a reply failure
  cannot cause the same text to be posted again on the next run;
* an image is a bonus, never a blocker — a failed upload still lets the text go
  out rather than losing the day's slot over decoration;
* a dry run reports the branch it would ACTUALLY take, because the link-reply
  branch costs an order of magnitude more and a dry run that misstates it is
  worse than no dry run.
"""

from __future__ import annotations

import datetime as dt

import pytest

from postvox.config.schema import (
    AccountConfig,
    XConfig,
    XGovernorConfig,
    XLinkReplyConfig,
    XMediaConfig,
    XPricingConfig,
    XShapeConfig,
)
from postvox.credentials import Credentials, StaticProvider
from postvox.http.client import HttpClient
from postvox.platforms.x.client import XClient
from postvox.platforms.x.governor import Governor
from postvox.state.counters import MemoryCounterStore

CREDS = {
    "X_API_KEY": "fake-key",
    "X_API_SECRET": "fake-secret",
    "X_ACCESS_TOKEN": "fake-token",
    "X_ACCESS_SECRET": "fake-token-secret",
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Records every call so the ORDER of operations can be asserted."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(200, {"data": {"id": "1111"}})

    def close(self):
        pass


def tweet_ok(post_id):
    return FakeResponse(200, {"data": {"id": post_id}})


def build(session, *, link_reply=False, accounts=None, creds=None, daily_cap=1,
          image_blocks=False, clock=None):
    config = XConfig(
        enabled=True,
        governor=XGovernorConfig(daily_cap=daily_cap, enabled_accounts=("moonlit",)),
        shape=XShapeConfig(),
        link_reply=XLinkReplyConfig(
            enabled=link_reply, cta="Full write-up, free", template="{cta} -> {url}"
        ),
        pricing=XPricingConfig(price_per_post=0.015, price_per_post_with_url=0.200,
                               currency="USD", verified_on="2031-01-01"),
        media=XMediaConfig(image_failure_blocks_post=image_blocks),
    )
    governor = Governor(
        config.governor, MemoryCounterStore(), counter_key="X_API_KEY",
        clock=clock or (lambda: dt.datetime(2031, 4, 9, 9, 0, tzinfo=dt.timezone.utc)),
    )
    client = XClient(
        config,
        credentials=Credentials([StaticProvider(CREDS if creds is None else creds)]),
        accounts=accounts if accounts is not None else {
            "moonlit": AccountConfig(name="moonlit", link_url="https://moonlit.example")
        },
        governor=governor,
        http=HttpClient(session=session),
    )
    # The OAuth 1.0a signer is an optional extra and is not what these tests are
    # about; the flow is.
    client._auth = lambda: ("fake-auth", None)  # type: ignore[assignment]
    return client, governor


CAPTION = "The Tower reversed means resisting a change that is already happening."


def test_post_is_counted_before_the_optional_link_reply():
    """The post is live before the reply is attempted, so it MUST be counted.

    A reply failure that skipped the counter would re-post the same text on the
    next run — paying twice to duplicate ourselves.
    """
    session = FakeSession([
        tweet_ok("2222"),                                  # the post: succeeds
        FakeResponse(403, None, "over capacity"),          # the link reply: fails
    ])
    client, governor = build(session, link_reply=True)

    result = client.auto_post("moonlit", CAPTION)

    assert result.ok is True, "the post is live; a failed reply is decoration"
    assert result.post_id == "2222"
    assert "link reply failed" in result.reason
    assert governor.posts_today() == 1, "the counter must not depend on the reply"

    # And the reply really was attempted, threaded under the post.
    assert len(session.calls) == 2
    assert session.calls[1]["kwargs"]["json"]["reply"] == {"in_reply_to_tweet_id": "2222"}


def test_link_reply_is_off_by_default_and_posts_no_url():
    session = FakeSession([tweet_ok("3333")])
    client, governor = build(session, link_reply=False)

    result = client.auto_post("moonlit", CAPTION + " https://moonlit.example/tower")

    assert result.ok
    assert len(session.calls) == 1, "no second, URL-bearing post"
    assert "https://" not in session.calls[0]["kwargs"]["json"]["text"]
    assert governor.posts_today() == 1


def test_a_failed_image_upload_never_blocks_the_text(tmp_path):
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"not really a jpeg")

    session = FakeSession([
        FakeResponse(400, None, "media upload rejected"),   # the image
        tweet_ok("4444"),                                   # the post, anyway
    ])
    client, governor = build(session)

    result = client.auto_post("moonlit", CAPTION, image=str(image))

    assert result.ok
    assert "image skipped" in result.reason
    assert governor.posts_today() == 1


def test_image_failure_can_be_made_blocking_for_callers_who_want_that(tmp_path):
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"not really a jpeg")
    session = FakeSession([FakeResponse(400, None, "media upload rejected")])
    client, governor = build(session, image_blocks=True)

    result = client.auto_post("moonlit", CAPTION, image=str(image))

    assert not result.ok
    assert governor.posts_today() == 0, "nothing was posted, so nothing is counted"


def test_a_video_is_never_uploaded_and_says_so(tmp_path):
    clip = tmp_path / "reel.mp4"
    clip.write_bytes(b"\x00\x00")
    session = FakeSession([tweet_ok("5555")])
    client, _ = build(session)

    result = client.auto_post("moonlit", CAPTION, image=str(clip))

    assert result.ok
    assert "stills only" in result.reason
    assert len(session.calls) == 1, "no media upload was attempted"


def test_dry_run_reports_the_actual_branch_and_costs_nothing():
    session = FakeSession()
    client, governor = build(session, link_reply=False)

    cheap = client.auto_post("moonlit", CAPTION, dry_run=True)
    assert cheap.ok
    assert ", no URL" in cheap.reason
    assert "0.015" in cheap.reason
    assert "+ link reply" not in cheap.reason

    expensive = client.auto_post("moonlit", CAPTION, dry_run=True, link_reply=True)
    assert "+ link reply (URL)" in expensive.reason
    assert "0.215" in expensive.reason, "the expensive branch must be priced as such"

    assert session.calls == [], "a dry run must not touch the network"
    assert governor.posts_today() == 0, "a dry run must not spend the daily slot"


def test_dry_run_says_so_when_prices_are_not_configured():
    session = FakeSession()
    client, _ = build(session)
    client.config = XConfig(
        enabled=True,
        governor=client.config.governor,
        pricing=XPricingConfig(),          # the shipped default: no figures
    )
    result = client.auto_post("moonlit", CAPTION, dry_run=True)
    assert "cost unknown" in result.reason


def test_missing_credentials_make_the_client_inert():
    session = FakeSession()
    client, governor = build(session, creds={"X_API_KEY": "only-one-of-four"})
    client._auth = XClient._auth.__get__(client)  # restore the real credential path

    result = client.auto_post("moonlit", CAPTION)

    assert not result.ok
    assert "X_API_SECRET" in result.reason
    assert session.calls == [], "an unconfigured platform posts nothing"
    assert governor.posts_today() == 0


def test_a_disallowed_account_is_refused_before_anything_else_happens():
    session = FakeSession()
    client, governor = build(session)

    result = client.auto_post("dailyverse", CAPTION)

    assert not result.ok
    assert "enabled_accounts" in result.reason
    assert session.calls == []


def test_a_thin_caption_never_spends_the_slot():
    session = FakeSession()
    client, governor = build(session)

    result = client.auto_post("moonlit", "Read more at https://moonlit.example")

    assert not result.ok
    assert "too thin" in result.reason
    assert session.calls == []
    assert governor.posts_today() == 0


def test_auto_post_never_raises():
    class ExplodingSession(FakeSession):
        def request(self, method, url, **kwargs):
            raise RuntimeError("network is on fire")

    client, governor = build(ExplodingSession())
    result = client.auto_post("moonlit", CAPTION)

    assert result.ok is False
    assert "request failed" in result.reason or "error:" in result.reason
    assert governor.posts_today() == 0


def test_the_body_sent_is_the_shaped_body():
    session = FakeSession([tweet_ok("6666")])
    client, _ = build(session)

    client.auto_post(
        "moonlit",
        CAPTION + "\nFull reading at https://moonlit.example #tarot #tower #daily",
    )

    sent = session.calls[0]["kwargs"]["json"]["text"]
    assert "https://" not in sent
    assert sent.count("#") == 2
    assert "Full reading at" not in sent
    assert len(sent) <= 280


@pytest.mark.parametrize("status", [200, 201])
def test_both_success_statuses_are_accepted(status):
    session = FakeSession([FakeResponse(status, {"data": {"id": "7777"}})])
    client, governor = build(session)
    result = client.auto_post("moonlit", CAPTION)
    assert result.ok
    assert governor.posts_today() == 1
