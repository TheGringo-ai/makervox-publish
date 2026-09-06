"""REGRESSION: a concurrent refresh must not spend a token that was already rotated.

THE INCIDENT THIS PINS
----------------------
TikTok rotates the refresh token ON USE: a successful refresh SPENDS the old
one. Two processes refreshing the same account race destructively — the loser
presents a token the platform has already retired, its refresh fails, and the
account needs a MANUAL re-authorization. Nothing recovers it.

Three defences, and each gets a test here:

1. the lock (exercised indirectly — the fake lock below is what "another process
   got there first" looks like);
2. **the re-read INSIDE the lock**: if the winner already refreshed, adopt THEIR
   token instead of spending ours. Asserted by proving NO token request is made;
3. **adopt-on-reject across machines**: a rejected refresh re-reads the shared
   store once and adopts a token another host just rotated.
"""

import contextlib
import json
import os
import time

import pytest

from postvox import Config
from postvox.errors import ReauthorizationRequired
from postvox.http import HttpClient, HttpPolicy
from postvox.platforms.tiktok.auth import TikTokAuth
from postvox.platforms.tiktok.errors import TikTokApiError
from postvox.state import token_store as token_store_module
from postvox.state.token_store import FileTokenStore, LockSettings


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._payload


class FakeSession:
    """Records every call and hands back queued responses."""

    def __init__(self, responses=()):
        self.headers = {}
        self.calls = []
        self.responses = list(responses)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP call: {0} {1}".format(method, url))
        item = self.responses.pop(0)
        return item if isinstance(item, FakeResponse) else FakeResponse(item)

    def close(self):
        pass


def make_config(tmp_path):
    return Config.from_mapping({
        "version": 1,
        "state": {"dir": str(tmp_path)},
        "accounts": {"moonlit": {"platforms": ["tiktok"]}},
        "platforms": {"tiktok": {
            "enabled": True,
            "redirect_uri": "http://127.0.0.1:8722/tiktok/callback",
        }},
        "token_stores": {"tiktok_tokens": {
            "impl": "postvox.state.token_store:FileTokenStore",
            "options": {"path": str(tmp_path / "tiktok_tokens.json"),
                        "lock": "tiktok-tokens"},
        }},
    })


def make_auth(tmp_path, monkeypatch, responses=()):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck-test-000000")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs-test-000000")
    config = make_config(tmp_path)
    session = FakeSession(responses)
    http = HttpClient(HttpPolicy(timeouts={"default": 5.0, "auth": 5.0}, attempts=1),
                      session=session)
    store = FileTokenStore(
        str(tmp_path / "tiktok_tokens.json"),
        lock="tiktok-tokens",
        locks=LockSettings.from_config(config.state),
    )
    auth = TikTokAuth(config.platforms.tiktok, token_store=store, http=http)
    return auth, store, session


def write_tokens(store, records):
    store.save(records)
    store.invalidate()


EXPIRED = {"open_id": "OID-1", "access_token": "AT-old",
           "refresh_token": "RT-old", "expires_at": time.time() - 60,
           "scope": "user.info.basic,video.upload"}


# --------------------------------------------------------------------------- #
# defence 2: re-read inside the lock
# --------------------------------------------------------------------------- #
def test_reread_inside_lock_adopts_the_winners_token(tmp_path, monkeypatch):
    auth, store, session = make_auth(tmp_path, monkeypatch, responses=[])
    write_tokens(store, {"moonlit": dict(EXPIRED)})

    winner = dict(EXPIRED, access_token="AT-winner", refresh_token="RT-winner",
                  expires_at=time.time() + 3600)

    @contextlib.contextmanager
    def lock_that_loses_the_race(name, directory, **kwargs):
        # While we were blocked on the lock, the other process finished its
        # refresh and wrote a brand-new token pair.
        with open(tmp_path / "tiktok_tokens.json", "w") as handle:
            json.dump({"moonlit": winner}, handle)

        class _Held:
            held = True

        yield _Held()

    monkeypatch.setattr(token_store_module, "file_lock", lock_that_loses_the_race)

    record = auth.refresh("moonlit")

    assert record["access_token"] == "AT-winner"
    assert record["refresh_token"] == "RT-winner"
    # THE POINT: our own refresh token was never presented, so it was never spent.
    assert session.calls == [], "a refresh was sent even though one had just landed"


def test_without_the_reread_the_token_would_be_spent(tmp_path, monkeypatch):
    """The same scenario with ``reread_inside_lock`` off really does spend it.

    Kept so the defence cannot be quietly disabled without a test going red.
    """
    auth, store, session = make_auth(tmp_path, monkeypatch, responses=[
        {"access_token": "AT-new", "refresh_token": "RT-new", "expires_in": 86400,
         "open_id": "OID-1", "scope": "user.info.basic,video.upload"},
    ])
    write_tokens(store, {"moonlit": dict(EXPIRED)})
    object.__setattr__(auth.cfg.tokens, "reread_inside_lock", False)

    auth.refresh("moonlit")

    assert len(session.calls) == 1, "the refresh token was NOT presented"
    sent = session.calls[0][2]["data"]["refresh_token"]
    assert sent == "RT-old", "this is the token that gets destructively spent"


# --------------------------------------------------------------------------- #
# defence 3: adopt a token another machine rotated
# --------------------------------------------------------------------------- #
def test_rejected_refresh_adopts_a_token_rotated_on_another_host(tmp_path, monkeypatch):
    auth, store, session = make_auth(tmp_path, monkeypatch, responses=[
        {"error": "invalid_grant",
         "error_description": "refresh token is invalid or expired"},
    ])
    write_tokens(store, {"moonlit": dict(EXPIRED)})

    other_host = dict(EXPIRED, access_token="AT-other", refresh_token="RT-other",
                      expires_at=time.time() + 3600)
    real_load = store.load
    state = {"calls": 0}

    def load_with_a_second_host(*args, **kwargs):
        state["calls"] += 1
        # Read 1 is the re-read inside the lock; read 2 happens after the
        # rejection, by which point the other host's rotation is visible.
        if state["calls"] >= 2:
            return {"moonlit": other_host}
        return real_load()

    monkeypatch.setattr(store, "load", load_with_a_second_host)

    record = auth.refresh("moonlit")

    assert record["access_token"] == "AT-other"
    assert record["refresh_token"] == "RT-other"


def test_rejected_refresh_with_nothing_newer_asks_for_reauthorization(tmp_path,
                                                                     monkeypatch):
    auth, store, _ = make_auth(tmp_path, monkeypatch, responses=[
        {"error": "invalid_grant", "error_description": "already used"},
    ])
    write_tokens(store, {"moonlit": dict(EXPIRED)})

    with pytest.raises(TikTokApiError) as excinfo:
        auth.refresh("moonlit")
    # The vendor's own message never says "re-authorize", so we must.
    assert "re-authorize" in str(excinfo.value).lower()
    assert "auth tiktok moonlit" in str(excinfo.value)


def test_missing_refresh_token_is_a_reauthorization_not_a_retry(tmp_path, monkeypatch):
    auth, store, _ = make_auth(tmp_path, monkeypatch)
    write_tokens(store, {"moonlit": {"access_token": "AT", "expires_at": 0}})

    with pytest.raises(ReauthorizationRequired):
        auth.refresh("moonlit")


# --------------------------------------------------------------------------- #
# the happy path still persists everything the response omitted
# --------------------------------------------------------------------------- #
def test_successful_refresh_keeps_fields_the_response_left_out(tmp_path, monkeypatch):
    auth, store, _ = make_auth(tmp_path, monkeypatch, responses=[
        {"access_token": "AT-new", "expires_in": 86400},   # no open_id, no scope
    ])
    write_tokens(store, {"moonlit": dict(EXPIRED)})

    record = auth.refresh("moonlit")

    assert record["access_token"] == "AT-new"
    assert record["open_id"] == "OID-1", "identity must survive a partial response"
    assert record["refresh_token"] == "RT-old", "kept when the response omits it"
    assert record["scope"] == "user.info.basic,video.upload"
    # The expiry carries a deliberate safety margin against clock skew.
    assert record["expires_at"] < time.time() + 86400


def test_token_file_is_owner_only(tmp_path, monkeypatch):
    _, store, _ = make_auth(tmp_path, monkeypatch)
    write_tokens(store, {"moonlit": dict(EXPIRED)})
    mode = os.stat(tmp_path / "tiktok_tokens.json").st_mode & 0o777
    assert mode == 0o600, "tokens are secrets"
