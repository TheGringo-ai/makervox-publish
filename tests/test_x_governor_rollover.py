"""REGRESSION: the stored date is read BEFORE it is overwritten.

The bug: ``same_day = state["date"] == today`` used to sit AFTER
``state["date"] = today``, so the comparison was always true, the midnight-reset
branch was unreachable and the counter climbed forever. The visible symptom was
NOT "no posts" — it was exactly ONE post per day, ever: the first run of the day
passed the guard on the stale date, incremented past the cap, and blocked every
later slot, while every log line still said the cap was working as configured.

A test that only posts once a day cannot see this. These drive the clock.
"""

from __future__ import annotations

import datetime as dt

import pytest

from postvox.config.schema import XGovernorConfig
from postvox.platforms.x.governor import Governor
from postvox.state.counters import JsonCounterStore, MemoryCounterStore


class Clock:
    """A settable clock, because the whole defect is about day boundaries."""

    def __init__(self, when: dt.datetime) -> None:
        self.now = when

    def __call__(self) -> dt.datetime:
        return self.now

    def advance_days(self, days: int) -> None:
        self.now = self.now + dt.timedelta(days=days)


def make_governor(store=None, clock=None, **overrides):
    config = XGovernorConfig(
        daily_cap=overrides.pop("daily_cap", 2),
        enabled_accounts=overrides.pop("enabled_accounts", ("moonlit",)),
        recent_keep=overrides.pop("recent_keep", 30),
        **overrides
    )
    return Governor(
        config,
        store if store is not None else MemoryCounterStore(),
        counter_key="X_API_KEY",
        clock=clock,
    )


def test_counter_resets_on_a_new_day():
    clock = Clock(dt.datetime(2031, 4, 9, 21, 0, tzinfo=dt.timezone.utc))
    gov = make_governor(clock=clock, daily_cap=1)

    assert gov.check("moonlit", "first body of the day").allowed
    gov.record("moonlit", "first body of the day")
    assert gov.posts_today() == 1

    # Same day: the cap binds.
    assert not gov.check("moonlit", "second body same day").allowed

    clock.advance_days(1)
    # New day: count RESETS to 0, not to "one more than yesterday".
    assert gov.posts_today() == 0
    assert gov.check("moonlit", "second body next day").allowed
    gov.record("moonlit", "second body next day")
    assert gov.posts_today() == 1, "a new day must reset, not accumulate"


def test_record_reads_the_stored_date_before_overwriting_it():
    """The exact regression, asserted on the stored document.

    Seed yesterday with a count far above the cap. If ``record`` overwrote the
    date before comparing, today's count would be 6 and the cap would be spent
    before the first post of the day even landed.
    """
    clock = Clock(dt.datetime(2031, 4, 9, 8, 0, tzinfo=dt.timezone.utc))
    store = MemoryCounterStore(
        {"X_API_KEY": {"date": "2031-04-08", "count": 5, "recent": ["oldmark"]}}
    )
    gov = make_governor(store=store, clock=clock, daily_cap=1)

    updated = gov.record("moonlit", "a fresh body")

    assert updated["date"] == "2031-04-09"
    assert updated["count"] == 1, "yesterday's count must not carry over"
    assert gov.posts_today() == 1
    # And the cap now binds for the rest of today, which is the intended shape.
    assert not gov.check("moonlit", "another body").allowed


def test_counter_increments_within_one_day_up_to_the_cap():
    clock = Clock(dt.datetime(2031, 4, 9, 9, 0, tzinfo=dt.timezone.utc))
    gov = make_governor(clock=clock, daily_cap=3)

    for index in range(3):
        body = "body number {0}".format(index)
        assert gov.check("moonlit", body).allowed
        gov.record("moonlit", body)

    assert gov.posts_today() == 3
    decision = gov.check("moonlit", "one too many")
    assert not decision.allowed
    assert "daily cap reached (3/3" in decision.reason


def test_near_duplicate_of_a_recent_post_is_suppressed():
    clock = Clock(dt.datetime(2031, 4, 9, 9, 0, tzinfo=dt.timezone.utc))
    gov = make_governor(clock=clock, daily_cap=5)

    gov.record("moonlit", "The Tower reversed means resisting change")
    clock.advance_days(1)

    # Same text with different emoji/punctuation is the SAME post.
    decision = gov.check("moonlit", "the tower reversed means resisting change!! ✨")
    assert not decision.allowed
    assert "near-duplicate" in decision.reason

    assert gov.check("moonlit", "A completely different sentence entirely").allowed


def test_recent_history_is_capped_at_recent_keep():
    clock = Clock(dt.datetime(2031, 4, 9, 9, 0, tzinfo=dt.timezone.utc))
    gov = make_governor(clock=clock, daily_cap=100, recent_keep=3)

    for index in range(5):
        gov.record("moonlit", "body number {0}".format(index))

    assert len(gov.recent()) == 3


def test_daily_cap_zero_disables_posting():
    gov = make_governor(daily_cap=0)
    decision = gov.check("moonlit", "anything at all")
    assert not decision.allowed
    assert "daily_cap is 0" in decision.reason


def test_the_counter_is_keyed_on_the_credential_set_not_the_brand(tmp_path):
    """Two brands sharing one set of credentials share ONE daily cap.

    A per-brand counter would let each brand spend its own "1 per day" onto the
    same platform account, doubling both the cadence and the bill while every
    check still read green.
    """
    clock = Clock(dt.datetime(2031, 4, 9, 9, 0, tzinfo=dt.timezone.utc))
    store = JsonCounterStore(str(tmp_path / "x_governor.json"))
    config = XGovernorConfig(daily_cap=1, enabled_accounts=("moonlit", "dailyverse"))
    gov = Governor(config, store, counter_key="X_API_KEY", clock=clock)

    assert gov.check("moonlit", "the first brand posts").allowed
    gov.record("moonlit", "the first brand posts")

    decision = gov.check("dailyverse", "the second brand tries")
    assert not decision.allowed, "a second brand must not get its own daily slot"
    assert "daily cap" in decision.reason


def test_json_store_round_trips_through_the_file(tmp_path):
    path = tmp_path / "nested" / "x_governor.json"
    clock = Clock(dt.datetime(2031, 4, 9, 9, 0, tzinfo=dt.timezone.utc))
    config = XGovernorConfig(daily_cap=1, enabled_accounts=("moonlit",))

    first = Governor(config, JsonCounterStore(str(path)), counter_key="X_API_KEY", clock=clock)
    first.record("moonlit", "a body")

    # A fresh process reads the same count back — the cap survives a restart.
    second = Governor(config, JsonCounterStore(str(path)), counter_key="X_API_KEY", clock=clock)
    assert second.posts_today() == 1
    assert not second.check("moonlit", "another body").allowed


def test_missing_state_directory_is_created_only_on_write(tmp_path):
    path = tmp_path / "made-on-write" / "x_governor.json"
    store = JsonCounterStore(str(path))

    assert store.read("X_API_KEY") == {}
    assert not path.parent.exists(), "reading must not create anything"

    store.write("X_API_KEY", {"date": "2031-04-09", "count": 1})
    assert path.exists()


@pytest.mark.parametrize("timezone_name", ["UTC", "Not/AZone"])
def test_an_unknown_timezone_falls_back_to_utc_without_failing(timezone_name):
    config = XGovernorConfig(daily_cap=1, enabled_accounts=("moonlit",),
                             timezone=timezone_name)
    gov = Governor(config, MemoryCounterStore(), counter_key="X_API_KEY")
    # A bad timezone shifts WHEN the day rolls over; it never lets an extra post
    # out, so it degrades to UTC with a warning rather than failing the publish.
    assert len(gov.today()) == 10
