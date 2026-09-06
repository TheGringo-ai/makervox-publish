"""The two allow-lists, and the two asymmetries that look like bugs.

Both of these are deliberate and both are the kind of thing a well-meaning
refactor "fixes" into something dangerous:

1. The ACCOUNT allow-list is deny-by-default. An EMPTY list denies EVERYTHING,
   not everything-allowed. A shared publish path otherwise grants posting rights
   to any account that merely passes through it, which is how one brand's
   content ends up queued onto another brand's account.
2. The SLUG allow-list is the other way around for MISSING data: a listed slug
   is allowed, an UNLISTED slug is denied, and a MISSING or empty slug is
   ALLOWED. Some callers never pass one, and refusing on missing metadata
   silently stops posting altogether — the hardest failure mode to notice.
"""

from __future__ import annotations

from postvox.config.schema import XGovernorConfig
from postvox.platforms.x.governor import Governor
from postvox.state.counters import MemoryCounterStore


def governor(**kwargs) -> Governor:
    return Governor(XGovernorConfig(**kwargs), MemoryCounterStore(), counter_key="X_API_KEY")


# --------------------------------------------------------------------------- #
# accounts: deny by default
# --------------------------------------------------------------------------- #
def test_empty_account_allow_list_denies_everything():
    gov = governor()  # enabled_accounts defaults to ()
    decision = gov.check("moonlit", "a perfectly good body")
    assert not decision.allowed
    assert "enabled_accounts" in decision.reason
    assert "empty allow-list denies every account" in decision.reason


def test_listed_account_is_allowed_and_unlisted_is_not():
    gov = governor(enabled_accounts=("moonlit",))
    assert gov.check("moonlit", "a perfectly good body").allowed
    assert not gov.check("dailyverse", "a perfectly good body").allowed


def test_account_check_needs_no_body_and_no_state():
    """check_policy is split out so a denied account never triggers a secret lookup."""
    gov = governor(enabled_accounts=("moonlit",))
    assert gov.check_policy("moonlit").allowed
    assert not gov.check_policy("dailyverse").allowed


# --------------------------------------------------------------------------- #
# slugs: unknown is allowed, unlisted is denied
# --------------------------------------------------------------------------- #
def test_empty_slug_list_means_no_slug_filtering_at_all():
    gov = governor(enabled_accounts=("moonlit",), allowed_slugs=())
    assert gov.check("moonlit", "body", slug="anything").allowed
    assert gov.check("moonlit", "body", slug=None).allowed


def test_listed_slug_is_allowed():
    gov = governor(enabled_accounts=("moonlit",), allowed_slugs=("howto", "reference"))
    assert gov.check("moonlit", "body", slug="howto").allowed


def test_unlisted_slug_is_denied():
    gov = governor(enabled_accounts=("moonlit",), allowed_slugs=("howto", "reference"))
    decision = gov.check("moonlit", "body", slug="affirmation")
    assert not decision.allowed
    assert "allowed_slugs" in decision.reason


def test_missing_slug_is_allowed_even_when_a_list_exists():
    """THE ASYMMETRY. Refusing on missing metadata silently stops posting."""
    gov = governor(enabled_accounts=("moonlit",), allowed_slugs=("howto",))
    assert gov.check("moonlit", "body", slug=None).allowed
    assert gov.check("moonlit", "body", slug="").allowed
    assert gov.check_policy("moonlit", None).allowed


def test_allow_lists_are_checked_before_the_daily_cap():
    """A denied account gets the reason it was denied, not a cap message."""
    gov = governor(enabled_accounts=(), daily_cap=0)
    decision = gov.check("moonlit", "body")
    assert "enabled_accounts" in decision.reason
    assert "daily_cap" not in decision.reason
