"""The shared Meta token document, including the v1 -> v2 key rename.

`accounts` was called `brands` in the v1 document. It is still read, so an
existing file keeps working untouched — and that has to stay true, because the
failure mode if it regresses is silent: an unreadable account map does not
error, it just makes every Page look unlinked, and the publisher reports "no
Facebook Page is linked" for an account that is perfectly well configured.

These were written after a live smoke test was briefly misread as showing the
alias was broken. It was not; the store file simply did not exist yet. Pinning
it means the next person does not have to re-derive that.
"""

from __future__ import annotations

from makervox_publish.platforms.meta.tokens import MetaTokenStore

PAGE_A, PAGE_B = "111111111111111", "222222222222222"


def test_v1_brands_key_is_still_read():
    """A v1 file, untranslated, must resolve its accounts."""
    doc = MetaTokenStore.normalize({
        "pages": {PAGE_A: {"token": "t", "name": "A"}},
        "brands": {"acct": PAGE_A},
    })
    assert doc["accounts"] == {"acct": PAGE_A}


def test_v2_accounts_key_is_read():
    doc = MetaTokenStore.normalize({
        "pages": {PAGE_A: {"token": "t"}},
        "accounts": {"acct": PAGE_A},
    })
    assert doc["accounts"] == {"acct": PAGE_A}


def test_a_v2_entry_wins_over_its_v1_twin():
    """Both keys present and disagreeing: the newer one is authoritative."""
    doc = MetaTokenStore.normalize({
        "pages": {PAGE_A: {}, PAGE_B: {}},
        "brands": {"acct": PAGE_A},
        "accounts": {"acct": PAGE_B},
    })
    assert doc["accounts"]["acct"] == PAGE_B


def test_v1_and_v2_entries_for_different_accounts_both_survive():
    doc = MetaTokenStore.normalize({
        "pages": {PAGE_A: {}, PAGE_B: {}},
        "brands": {"old": PAGE_A},
        "accounts": {"new": PAGE_B},
    })
    assert doc["accounts"] == {"old": PAGE_A, "new": PAGE_B}


def test_missing_top_level_keys_are_filled_in():
    """An empty or partial document must not KeyError downstream."""
    doc = MetaTokenStore.normalize({})
    assert doc == {"pages": {}, "accounts": {}, "instagram": {}}


def test_wrong_types_are_replaced_rather_than_trusted():
    doc = MetaTokenStore.normalize({"pages": None, "accounts": [], "instagram": "x"})
    assert doc == {"pages": {}, "accounts": {}, "instagram": {}}


def test_normalize_does_not_mutate_the_input():
    """The caller's document is shared state; normalising must not edit it."""
    original = {"pages": {PAGE_A: {"token": "t"}}, "brands": {"acct": PAGE_A}}
    MetaTokenStore.normalize(original)
    assert "accounts" not in original, "normalize edited the caller's document"
    assert original["brands"] == {"acct": PAGE_A}
