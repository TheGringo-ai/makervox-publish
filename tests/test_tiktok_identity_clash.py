"""REGRESSION: an authorization for the wrong account is REFUSED, not filed.

THE INCIDENT
------------
The platform issues a token for whichever account the BROWSER was signed into,
not the one named on the command line. Authorizing label B while still signed in
as label A silently saves A's credentials under B. Every later post for B then
lands on A's account, and both labels share A's pending-share quota — so the
upload cap is reached twice as fast and the cause is invisible, because every
call still "succeeds".

The tell is the platform account id (``open_id``) coming back for a label that
already belongs to a different one. The save is refused there.
"""

import json

import pytest

from makervox_publish import Config
from makervox_publish.errors import IdentityClash
from makervox_publish.http import HttpClient, HttpPolicy
from makervox_publish.platforms.tiktok.auth import TikTokAuth
from makervox_publish.state.token_store import FileTokenStore, LockSettings


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
        return FakeResponse(self.responses.pop(0))

    def close(self):
        pass


def build(tmp_path, monkeypatch, responses):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck-test-000000")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs-test-000000")
    config = Config.from_mapping({
        "version": 1,
        "state": {"dir": str(tmp_path)},
        "accounts": {"moonlit": {"platforms": ["tiktok"]},
                     "dailyverse": {"platforms": ["tiktok"]}},
        "platforms": {"tiktok": {
            "enabled": True,
            "redirect_uri": "http://127.0.0.1:8722/tiktok/callback",
        }},
        "token_stores": {"tiktok_tokens": {
            "impl": "makervox_publish.state.token_store:FileTokenStore",
            "options": {"path": str(tmp_path / "tiktok_tokens.json")},
        }},
    })
    store = FileTokenStore(str(tmp_path / "tiktok_tokens.json"),
                           lock="tiktok-tokens",
                           locks=LockSettings.from_config(config.state))
    http = HttpClient(HttpPolicy(timeouts={"default": 5.0, "auth": 5.0}, attempts=1),
                      session=FakeSession(responses))
    return TikTokAuth(config.platforms.tiktok, token_store=store, http=http), store


def test_authorizing_while_signed_in_as_another_account_is_refused(tmp_path,
                                                                   monkeypatch):
    auth, store = build(tmp_path, monkeypatch, [
        {"access_token": "AT-a", "refresh_token": "RT-a", "expires_in": 86400,
         "open_id": "OID-A", "scope": "user.info.basic,video.upload"},
    ])
    # "moonlit" is already connected to platform account OID-A.
    store.save({"moonlit": {"open_id": "OID-A", "access_token": "AT-a",
                            "refresh_token": "RT-a", "expires_at": 9e12,
                            "scope": "user.info.basic,video.upload"}})
    store.invalidate()

    # Now someone runs the auth flow for "dailyverse" but the browser is still
    # signed in as the account behind "moonlit".
    with pytest.raises(IdentityClash) as excinfo:
        auth.exchange_code("dailyverse", "the-code")

    message = str(excinfo.value)
    assert "moonlit" in message and "dailyverse" in message
    assert "OID-A" in message

    # And nothing was written: "dailyverse" must not exist pointing at OID-A.
    store.invalidate()
    assert "dailyverse" not in store.load()


def test_reauthorizing_the_same_label_is_allowed(tmp_path, monkeypatch):
    """Re-auth of the SAME label with the SAME platform account is normal."""
    auth, store = build(tmp_path, monkeypatch, [
        {"access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 86400,
         "open_id": "OID-A", "scope": "user.info.basic,user.info.stats,video.upload"},
    ])
    store.save({"moonlit": {"open_id": "OID-A", "access_token": "AT-1",
                            "refresh_token": "RT-1", "expires_at": 1.0,
                            "scope": "user.info.basic,video.upload"}})
    store.invalidate()

    record = auth.exchange_code("moonlit", "the-code")

    assert record["access_token"] == "AT-2"
    # Scopes are frozen at authorization, so a RE-AUTH is the only thing that
    # can widen them. The stored scope string must reflect the new grant.
    assert "user.info.stats" in record["scope"]


def test_a_genuinely_new_account_saves_normally(tmp_path, monkeypatch):
    auth, store = build(tmp_path, monkeypatch, [
        {"access_token": "AT-b", "refresh_token": "RT-b", "expires_in": 86400,
         "open_id": "OID-B", "scope": "user.info.basic,video.upload"},
    ])
    store.save({"moonlit": {"open_id": "OID-A", "access_token": "AT-a",
                            "refresh_token": "RT-a", "expires_at": 9e12}})
    store.invalidate()

    auth.exchange_code("dailyverse", "the-code")

    store.invalidate()
    saved = store.load()
    assert saved["dailyverse"]["open_id"] == "OID-B"
    assert saved["moonlit"]["open_id"] == "OID-A", "the other label is untouched"
