"""REGRESSION: hashtags are stripped BEFORE the dangling-connector sweep.

This is the ordering bug that gave the module its trace API. Captions put their
tags AFTER the link, so with the tags still present the leftover "Full reading
at" sits mid-string and the right-anchored sweep never fires. Get the order
wrong and posts ship ending in a preposition — while a test that only inspects
the FINAL string still passes, because the final string ends in hashtags either
way.

So these tests assert MID-pipeline, on ShapeTrace, not just on the output.
"""

from __future__ import annotations

from makervox_publish.text.shape import (
    DEFAULT_TRAILING_CONNECTORS,
    fingerprint,
    shape,
    shape_trace,
    strip_dangling,
)

CAPTION = (
    "The Tower reversed means resisting a change that is already happening.\n"
    "Full reading at https://example.test/tarot/tower #tarot #tower #daily"
)


def test_hashtags_are_removed_before_the_dangling_sweep():
    trace = shape_trace(CAPTION, max_hashtags=2)

    # The URL goes first...
    assert "https://" not in trace.after_urls

    # ...then the tags, which is the step under test. If the sweep ran first,
    # after_hashtags would still end in "#daily" and after_dangling would still
    # contain the orphaned "at".
    assert "#tarot" not in trace.after_hashtags
    assert trace.hashtags == ("#tarot", "#tower", "#daily")

    # THE ASSERTION THAT MATTERS: with the tags gone, "at" is genuinely trailing
    # and the sweep can see it.
    assert trace.after_hashtags.rstrip().endswith("at")
    # The whole CTA phrase comes off, not just the preposition: "at", then
    # "reading", then "Full", stopping at the sentence that ends in a full stop.
    assert trace.after_dangling.endswith("happening.")
    assert "Full reading" not in trace.after_dangling


def test_final_string_alone_cannot_catch_the_order_bug():
    """Documents WHY the trace exists: the output ends in hashtags regardless."""
    final = shape(CAPTION, max_hashtags=2)
    assert final.endswith("#tarot #tower")
    # ...which is why the real check is the mid-pipeline one above.


def test_only_trailing_connectors_are_dropped():
    # "at dawn" is content: the connector has an object, so it stays.
    assert strip_dangling("Read the cards at dawn") == "Read the cards at dawn"
    # A bare connector at the end pointed at a link that is now gone.
    assert strip_dangling("Read the cards at") == "Read the cards"
    # Several in a row come off one token at a time.
    assert strip_dangling("The Tower reversed. Full reading at") == "The Tower reversed."
    # A caption that is NOTHING but a CTA collapses to empty — and the client's
    # min_body_chars check then refuses to spend the day's slot on it, which is
    # the correct outcome rather than a bug.
    assert strip_dangling("Full reading link in bio") == ""
    # A sentence-ending token is content even when the word is in the list.
    assert strip_dangling("That is all it.") == "That is all it."
    # Arrows and separators are not in the configurable word list, but go too:
    # a caption in any language that ends in "→" is dangling for the same reason.
    assert strip_dangling("The Tower reversed →") == "The Tower reversed"
    # "Read more →" is ALL connector, so it collapses entirely.
    assert strip_dangling("Read more →") == ""


def test_connector_list_is_configurable_and_english_only_by_default():
    assert "at" in DEFAULT_TRAILING_CONNECTORS
    # A caller with another locale supplies their own set; the default does not
    # touch a word it was never given.
    assert strip_dangling("Lectura completa en", connectors=()) == "Lectura completa en"
    assert strip_dangling("Lectura completa en", connectors=("en",)) == "Lectura completa"


def test_hashtag_cap_and_length_limit():
    body = shape(CAPTION, max_hashtags=2)
    assert body.count("#") == 2, "a hashtag wall is a spam signal on this platform"
    assert len(body) <= 280

    long_caption = ("Sentence one is quite long and says something. " * 12)
    body = shape(long_caption)
    assert len(body) <= 280
    # Prefers a sentence boundary over a mid-word chop.
    assert body.endswith(".")


def test_hashtags_are_dropped_rather_than_the_body_when_they_do_not_fit():
    filler = "x" * 275
    body = shape(filler + " #one #two", max_hashtags=2)
    assert "#one" not in body
    assert body.startswith("xxx")


def test_fingerprint_ignores_emoji_and_punctuation_churn():
    assert fingerprint("This is your sign ✨") == fingerprint("This is your sign!")
    assert fingerprint("The Tower, reversed.") == fingerprint("the tower reversed")
