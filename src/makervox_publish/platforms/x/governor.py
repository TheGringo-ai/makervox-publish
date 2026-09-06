"""The posting governor: a daily cap, two allow-lists and duplicate suppression.

⚠️ Suppression on this platform is ONE-WAY. An account that behaves like a
marketing bot gets suppressed, and posting MORE cannot undo it. Every rule here
trades reach per post for the account staying visible at all, and every default
is the conservative one.

Five rules, each with a reason that is not obvious from the code:

1. **ONE post per ACCOUNT per day** (``daily_cap``). Not per brand. One set of
   credentials is one platform account, so a per-brand counter lets two brands
   each spend their own "1 per day" onto the SAME account — doubling both the
   cadence and the bill while every check still reads green. The counter is
   therefore keyed on the CREDENTIAL SET, not on the caller's label.
2. **Accounts must be allow-listed** (``enabled_accounts``), and an EMPTY LIST
   DENIES EVERYTHING. A shared publish path otherwise grants posting rights to
   any brand that merely passes through it, which is how content from one brand
   comes to be queued onto another brand's account.
3. **Content kinds are allow-listed** (``allowed_slugs``) with a deliberate
   asymmetry: a listed slug is allowed, an UNLISTED slug is DENIED, and a
   MISSING slug is ALLOWED. Some callers never pass one, and refusing on missing
   metadata silently stops posting altogether — the hardest failure to notice.
4. **Near-duplicate suppression** against the last ``recent_keep`` posts.
   Re-posting similar text is what gets a feed's reach throttled.
5. **No URL in the body by default** — that rule lives in the client, because it
   is about price, not cadence.

The counter itself is a :class:`~makervox_publish.state.counters.CounterStore`; see that
module for why the JSON default is single-writer only when what it guards costs
money per call.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

from makervox_publish.logging import get_logger
from makervox_publish.state.counters import CounterStore, JsonCounterStore
from makervox_publish.text.shape import fingerprint

__all__ = ["GovernorDecision", "Governor"]

log = get_logger(__name__)


@dataclass(frozen=True)
class GovernorDecision:
    """Why a post may or may not go out. Unpacks as ``(allowed, reason)``."""

    allowed: bool
    reason: str = ""
    posts_today: int = 0
    date: str = ""

    def __bool__(self) -> bool:
        return bool(self.allowed)

    def __iter__(self) -> Iterator[Any]:
        yield self.allowed
        yield self.reason


class Governor:
    """Decides whether one more post may go out, and records the ones that do.

    :meth:`check` never mutates. :meth:`record` mutates once, AFTER the post is
    live — see the ordering note in :meth:`record`.
    """

    def __init__(
        self,
        config,
        store: Optional[CounterStore] = None,
        *,
        counter_key: str = "account",
        clock=None,
        logger=None,
    ) -> None:
        self.config = config
        self.store = store if store is not None else JsonCounterStore("makervox_publish_x_governor.json")
        #: The counter belongs to the PLATFORM ACCOUNT, not to a brand. Callers
        #: that genuinely hold several sets of credentials give each set its own
        #: key (the client derives it from the credential NAMES, so two configs
        #: pointing at two different apps do not share a cap).
        self.counter_key = counter_key
        self._clock = clock
        self._log = logger or log

    @classmethod
    def from_config(cls, x_config, *, store: Optional[CounterStore] = None,
                    counter_key: Optional[str] = None, clock=None) -> "Governor":
        """Build from a :class:`makervox_publish.config.schema.XConfig`.

        The counter store comes from ``platforms.x.governor.state.impl``; the key
        defaults to the api-key CREDENTIAL NAME, which is the closest thing the
        config has to "which platform account is this".
        """
        governor_cfg = x_config.governor
        built = store if store is not None else governor_cfg.state.build()
        key = counter_key or getattr(x_config, "api_key_credential", "account")
        return cls(governor_cfg, built, counter_key=key, clock=clock)

    # -- clock -------------------------------------------------------------- #
    def _now(self) -> dt.datetime:
        if self._clock is not None:
            return self._clock()
        tz = self._timezone()
        return dt.datetime.now(tz)

    def _timezone(self):
        name = (getattr(self.config, "timezone", "") or "UTC").strip()
        if name.upper() == "UTC":
            return dt.timezone.utc
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception as exc:  # noqa: BLE001 - missing tzdata is common on Windows
            # Falling back to UTC shifts WHEN the day rolls over; it never lets
            # an extra post out, so it is a warning rather than a failure.
            self._log.warning(
                "timezone %r is unavailable (%s); using UTC for the daily "
                "rollover. Install tzdata to honour it.", name, exc,
            )
            return dt.timezone.utc

    def today(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    # -- reads -------------------------------------------------------------- #
    def snapshot(self) -> Dict[str, Any]:
        """The stored counter document. Never mutates."""
        return self.store.read(self.counter_key)

    def posts_today(self) -> int:
        state = self.snapshot()
        return int(state.get("count") or 0) if state.get("date") == self.today() else 0

    def recent(self) -> List[str]:
        value = self.snapshot().get("recent") or []
        return [str(item) for item in value]

    # -- the decision ------------------------------------------------------- #
    def check_policy(self, account: str, slug: Optional[str] = None) -> GovernorDecision:
        """The two allow-lists only — no state read, no body needed.

        Split out so a caller can refuse a disallowed account BEFORE resolving
        credentials or shaping text: an account that can never post should not
        cause a secret lookup, and the reason it cannot post does not depend on
        what it was going to say.
        """
        today = self.today()

        if not self.config.account_allowed(account):
            # Deny-by-default. Naming the config key matters: the failure looks
            # identical to "misconfigured" and to "deliberately off".
            return GovernorDecision(
                False,
                "account {0!r} is not in platforms.x.governor.enabled_accounts "
                "(an empty allow-list denies every account)".format(account),
                date=today,
            )

        if not self.config.slug_allowed(slug):
            return GovernorDecision(
                False,
                "content kind {0!r} is not in platforms.x.governor.allowed_slugs".format(slug),
                date=today,
            )

        return GovernorDecision(True, "", date=today)

    def check(self, account: str, body: str, slug: Optional[str] = None) -> GovernorDecision:
        """May this body go out for this account right now?

        Runs the allow-lists, then the daily cap, then near-duplicate
        suppression. Never mutates: :meth:`record` is the only writer.
        """
        today = self.today()

        policy = self.check_policy(account, slug)
        if not policy.allowed:
            return policy

        state = self.snapshot()
        count = int(state.get("count") or 0) if state.get("date") == today else 0
        cap = int(getattr(self.config, "daily_cap", 1))
        if cap <= 0:
            return GovernorDecision(False, "daily_cap is 0 — posting is disabled",
                                    posts_today=count, date=today)
        if count >= cap:
            return GovernorDecision(
                False,
                "daily cap reached ({0}/{1} today) — deliberate".format(count, cap),
                posts_today=count, date=today,
            )

        mark = fingerprint(body)
        if mark and mark in (state.get("recent") or []):
            return GovernorDecision(
                False, "near-duplicate of a recent post — skipped",
                posts_today=count, date=today,
            )

        return GovernorDecision(True, "", posts_today=count, date=today)

    # -- the write ---------------------------------------------------------- #
    def record(self, account: str, body: str) -> Dict[str, Any]:
        """Count one delivered post.

        Call this the moment the post is LIVE and BEFORE any optional follow-up
        (a threaded link reply, an insight fetch). The post is already published
        by then, and a follow-up failure that skipped the counter would re-post
        the same text on the next run — paying twice to duplicate ourselves.
        """
        today = self.today()
        mark = fingerprint(body)
        keep = int(getattr(self.config, "recent_keep", 30))
        stamp = self._now().isoformat(timespec="seconds")

        def bump(state: Dict[str, Any]) -> Dict[str, Any]:
            # READ THE STORED DATE BEFORE OVERWRITING IT. This comparison used
            # to sit AFTER `state["date"] = today`, which made it always true,
            # so the midnight-reset branch was unreachable and the counter
            # climbed forever. The cap then let exactly ONE post through per
            # day, ever: the first run passed the guard on the stale date,
            # incremented past the cap, and blocked every later slot — while
            # every log line still said the cap was working as configured.
            same_day = state.get("date") == today
            state["date"] = today
            state["count"] = (int(state.get("count") or 0) + 1) if same_day else 1
            if mark:
                state["recent"] = ([mark] + list(state.get("recent") or []))[:keep]
            state["last_account"] = account
            state["last_post_at"] = stamp
            return state

        updated = self.store.update(self.counter_key, bump)
        self._log.info(
            "x: recorded post %d/%s for %r on %s",
            int(updated.get("count") or 0),
            getattr(self.config, "daily_cap", "?"), account, today,
        )
        return updated

    def describe(self) -> str:
        return "governor(cap={0}/day, key={1!r}, store={2})".format(
            getattr(self.config, "daily_cap", "?"),
            self.counter_key,
            getattr(self.store, "describe", lambda: type(self.store).__name__)(),
        )
