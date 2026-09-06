"""The first-comment race, which is the most expensive bug in the Meta surface.

Two separate failures stack here, and pinning only one of them is what let the
original bug survive:

* the object you must comment on is NOT the id the reel finish phase returns.
  It is ``{page_id}_{story_fbid}``, where the story fbid comes from the VIDEO
  NODE'S own ``post_id`` field. Commenting on the finish id, or the bare video
  id, returns ``(#100) Unsupported post request``.
* even once that id resolves, the object still will not ACCEPT a comment for a
  few seconds. Resolving is not enough — the POST itself has to be retried.

Measured in the code this was extracted from: the call-to-action had landed on
7 of 288 reels, and every success was a run where id resolution happened to be
slow enough that the object settled in the meantime. That is what identified it
as a race rather than a permissions problem.

A failed comment must NEVER fail a published post — the reel is already live,
and raising here would turn a cosmetic miss into a broken pipeline.
"""

from __future__ import annotations

import pytest

from makervox_publish.errors import PublishError
from makervox_publish.platforms.meta.facebook import FacebookPublisher
from makervox_publish.platforms.meta.tokens import PageCredentials

PAGE_ID = "111111111111111"
VIDEO_ID = "222222222222222"
STORY_FBID = "333333333333333"
#: What the Graph API actually wants. Not the video id, not the finish post_id.
TARGET = "{0}_{1}".format(PAGE_ID, STORY_FBID)


class FakeResolver:
    def resolve(self, account):
        return PageCredentials(page_id=PAGE_ID, token="fake-page-token")


class FakeGraph:
    """Records calls and replays a scripted sequence of responses.

    `get_script` / `post_script` are lists; each entry is either a dict to
    return or an Exception to raise.
    """

    def __init__(self, get_script=None, post_script=None):
        self.get_script = list(get_script or [])
        self.post_script = list(post_script or [])
        self.gets, self.posts = [], []

    def _next(self, script, default):
        if not script:
            return default
        item = script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def get(self, path, token, params=None, **kw):
        self.gets.append((path, params))
        return self._next(self.get_script, {})

    def post(self, path, token, data=None, **kw):
        self.posts.append((path, data))
        return self._next(self.post_script, {"id": "comment-id"})

    def error(self, payload):
        return str(payload)

    def failed(self, payload):
        return "id" not in (payload or {})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """The retries sleep 5+10+15+20s. Tests must not actually wait."""
    monkeypatch.setattr("makervox_publish.platforms.meta.facebook.time.sleep",
                        lambda _s: None)


def _pub(graph):
    return FacebookPublisher(graph, FakeResolver())


# ------------------------------------------------------- resolving the target #

def test_comment_target_is_page_underscore_story_not_the_video_id():
    graph = FakeGraph(get_script=[{"post_id": STORY_FBID}])
    target = _pub(graph).reel_comment_target("acct", VIDEO_ID)
    assert target == TARGET
    # and it must be built from a fetch of the VIDEO NODE's post_id field
    path, params = graph.gets[0]
    assert path == VIDEO_ID
    assert params == {"fields": "post_id"}


def test_comment_target_retries_while_post_id_lags():
    """post_id can be absent for a beat after publish; that is not a failure."""
    graph = FakeGraph(get_script=[{}, {}, {"post_id": STORY_FBID}])
    assert _pub(graph).reel_comment_target("acct", VIDEO_ID, attempts=5) == TARGET
    assert len(graph.gets) == 3


def test_comment_target_gives_up_and_returns_none():
    graph = FakeGraph(get_script=[{}, {}, {}])
    assert _pub(graph).reel_comment_target("acct", VIDEO_ID, attempts=3) is None


# ------------------------------------------------------- retrying the POST #

def test_first_comment_retries_the_post_not_just_the_lookup():
    """THE bug. The id resolves first time, and the POST still has to retry.

    Pinned separately from the lookup retry because fixing only the lookup is
    what left the CTA landing on 7 of 288 reels.
    """
    graph = FakeGraph(
        get_script=[{"post_id": STORY_FBID}],
        post_script=[
            PublishError("(#100) Unsupported post request"),
            PublishError("(#100) Unsupported post request"),
            {"id": "comment-42"},
        ],
    )
    got = _pub(graph).add_first_comment("acct", VIDEO_ID, "the CTA")
    assert got == "comment-42"
    assert len(graph.posts) == 3, "the POST must be retried, not just the lookup"
    assert graph.posts[0][0] == "{0}/comments".format(TARGET)
    assert graph.posts[0][1] == {"message": "the CTA"}


def test_first_comment_returns_none_rather_than_raising():
    """A missing comment must not fail a reel that is already published."""
    graph = FakeGraph(
        get_script=[{"post_id": STORY_FBID}],
        post_script=[PublishError("insufficient permissions")] * 8,
    )
    assert _pub(graph).add_first_comment("acct", VIDEO_ID, "the CTA") is None


def test_first_comment_skips_cleanly_when_target_never_resolves():
    graph = FakeGraph(get_script=[{}] * 8)
    assert _pub(graph).add_first_comment("acct", VIDEO_ID, "the CTA") is None
    assert graph.posts == [], "must not POST to an unresolved object"


# --------------------------------------------------------------- page lookup #

def test_unlinked_account_raises_rather_than_downgrading():
    """A resolver returning None means 'not linked' and must be loud.

    A silent skip here reads exactly like an unconfigured account, which is how
    a broken lookup hides for weeks.
    """
    class Unlinked:
        def resolve(self, account):
            return None

    pub = FacebookPublisher(FakeGraph(), Unlinked())
    with pytest.raises(PublishError) as e:
        pub.page("acct")
    assert "no Facebook Page is linked" in str(e.value)


def test_no_marketing_copy_ships_by_default():
    """The package must carry no account names or CTA text of its own."""
    assert _pub(FakeGraph()).first_comments == {}
