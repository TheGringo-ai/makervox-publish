"""Reading back counters, and the two traps in doing so.

* **A scope error is a RE-AUTH, not a retry.** Scopes are frozen at
  authorization and refreshing never widens them; the platform's own message
  does not say so, so the exception must.
* **Never two follower rows for one day.** The job may run more than once, and a
  duplicate day silently turns a flat day into a fake datapoint the moment
  anyone plots the series.
"""

import json
import time

import pytest

from postvox import Config
from postvox.errors import ReauthorizationRequired
from postvox.http import HttpClient, HttpPolicy
from postvox.platforms.tiktok import TikTokClient


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self.calls = []
        self.responses = list(responses)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP call: {0} {1}".format(method, url))
        return FakeResponse(self.responses.pop(0))

    def close(self):
        pass


def build(tmp_path, monkeypatch, responses, metrics=None):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck-test-000000")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs-test-000000")
    tiktok = {"enabled": True,
              "redirect_uri": "http://127.0.0.1:8722/tiktok/callback"}
    if metrics:
        tiktok["metrics"] = metrics
    config = Config.from_mapping({
        "version": 1,
        "state": {"dir": str(tmp_path / "state")},
        "accounts": {"moonlit": {"platforms": ["tiktok"]}},
        "platforms": {"tiktok": tiktok},
        "token_stores": {"tiktok_tokens": {
            "impl": "postvox.state.token_store:FileTokenStore",
            "options": {"path": str(tmp_path / "state" / "tiktok_tokens.json")},
        }},
    })
    session = FakeSession(responses)
    http = HttpClient(HttpPolicy(timeouts={"default": 5.0, "insights": 5.0},
                                 attempts=1), session=session)
    client = TikTokClient.from_config(config, http=http)
    client.auth.store.save({"moonlit": {
        "open_id": "OID-A", "access_token": "AT", "refresh_token": "RT",
        "expires_at": time.time() + 3600,
        "scope": "user.info.basic,user.info.stats,video.list",
    }})
    client.auth.store.invalidate()
    return client, session


STATS_OK = {"data": {"user": {"follower_count": 1234, "following_count": 7,
                              "likes_count": 999, "video_count": 42}},
            "error": {"code": "ok"}}

SCOPE_DENIED = {"error": {"code": "scope_not_authorized",
                          "message": "The access token is missing a scope",
                          "log_id": "LOG-7"}}


def test_stats_returns_the_user_block(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch, [STATS_OK])
    assert client.stats("moonlit")["follower_count"] == 1234


def test_a_scope_denial_says_reauthorize_not_retry(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch, [SCOPE_DENIED])
    with pytest.raises(ReauthorizationRequired) as excinfo:
        client.stats("moonlit")
    message = str(excinfo.value)
    assert "user.info.stats" in message
    assert "refreshing does NOT add them" in message


def test_video_list_scope_denial_names_video_list(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch, [SCOPE_DENIED])
    with pytest.raises(ReauthorizationRequired) as excinfo:
        client.list_videos("moonlit")
    assert "video.list" in str(excinfo.value)


def test_page_size_is_clamped_to_the_api_maximum(tmp_path, monkeypatch):
    client, session = build(tmp_path, monkeypatch, [
        {"data": {"videos": [], "cursor": None, "has_more": False},
         "error": {"code": "ok"}},
    ])
    client.list_videos("moonlit", max_count=200)
    assert session.calls[0][2]["json"]["max_count"] == 20


def test_all_videos_stops_on_a_repeated_cursor(tmp_path, monkeypatch):
    """An unbounded pager against a metered API is a billing incident."""
    page = {"data": {"videos": [{"id": "1"}], "cursor": 111, "has_more": True},
            "error": {"code": "ok"}}
    client, session = build(tmp_path, monkeypatch, [page, page, page])
    videos = client.all_videos("moonlit", limit=100)
    assert len(videos) == 2, "stopped once the cursor stopped advancing"
    assert len(session.calls) == 2


def test_follower_log_writes_one_row_per_day(tmp_path, monkeypatch):
    log_path = tmp_path / "followers.csv"
    client, _ = build(tmp_path, monkeypatch, [STATS_OK, STATS_OK],
                      metrics={"follower_log": {"enabled": True,
                                                "path": str(log_path)}})

    assert client.record_followers("moonlit", when="2031-04-09") == 1234
    assert client.record_followers("moonlit", when="2031-04-09") == 1234

    rows = log_path.read_text().splitlines()
    assert rows[0] == "date,account,followers,source"
    assert rows[1] == "2031-04-09,moonlit,1234,api"
    assert len(rows) == 2, (
        "a duplicate day turns a flat day into a fake datapoint when plotted"
    )


def test_follower_log_is_off_by_default_and_writes_nothing(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch, [STATS_OK])
    assert client.record_followers("moonlit", when="2031-04-09") == 1234
    state_dir = tmp_path / "state"
    written = list(state_dir.glob("*.csv")) if state_dir.exists() else []
    assert written == [], "the package writes no series unless asked to"
