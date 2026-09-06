"""A thin ``requests`` wrapper with one timeout policy and one retry rule.

THE RETRY RULE, AND WHY IT IS THIS NARROW
-----------------------------------------
**Only transport-level failures retry**: connect and read timeouts, DNS
failures, connection resets. An API REJECTION IS NEVER RETRIED. That is not
timidity, it is two separate scars:

* Re-sending an upload that was refused for spam or rate reasons makes the
  rejection worse — a pending-share queue that is already at its cap does not
  clear because you asked again, and the platform reads the repetition as the
  behaviour it was throttling.
* On at least one platform every attempt costs money, so a retry loop around a
  4xx is a billing loop.

``retry_on`` can be widened to ``5xx`` or ``429`` by an operator who knows their
own workload. It is not the default, and the config comment says so.

TIMEOUTS ARE PER OPERATION CLASS
--------------------------------
A token exchange and a 20 MiB chunk PUT do not deserve the same budget. The
classes come from ``[http.timeouts_s]``: ``auth``, ``small_write``,
``media_create``, ``poll``, ``upload_chunk``, ``insights``, ``default``. Every
request names one; an unnamed class falls back to ``default`` rather than to no
timeout at all, because a request with no timeout is how a scheduled job wedges
forever, and a job that never returns never logs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import requests

from makervox_publish.errors import PostvoxError
from makervox_publish.logging import get_logger

__all__ = ["HttpPolicy", "HttpClient", "TransportError"]

log = get_logger(__name__)

_TIMEOUT_KINDS = (
    "default", "auth", "small_write", "media_create", "poll", "upload_chunk",
    "insights",
)


class TransportError(PostvoxError):
    """The request never produced an HTTP response.

    Distinct from an API rejection on purpose: this one is safe to retry and an
    API rejection is not.
    """

    def __init__(self, method: str, url: str, attempts: int, cause: BaseException) -> None:
        super().__init__(
            "{0} {1} failed after {2} attempt(s): {3}: {4}".format(
                method.upper(), _redact_url(url), attempts, type(cause).__name__, cause
            )
        )
        self.method = method
        self.url = url
        self.attempts = attempts
        self.cause = cause


def _redact_url(url: str) -> str:
    """Drop the query string.

    Upload URLs carry signed parameters, and a signed upload URL in a log line
    is a credential in a log line.
    """
    return url.split("?", 1)[0]


@dataclass(frozen=True)
class HttpPolicy:
    """Timeouts and retry behaviour, resolved once from ``[http]``."""

    user_agent: str = "makervox_publish"
    timeouts: Dict[str, float] = field(default_factory=dict)
    attempts: int = 3
    backoff_s: float = 3.0
    retry_on: Tuple[str, ...] = ("transport",)

    @classmethod
    def from_config(cls, http_cfg) -> "HttpPolicy":
        """Build from a :class:`makervox_publish.config.HttpConfig`."""
        timeouts_cfg = getattr(http_cfg, "timeouts_s", None)
        timeouts = {}
        for kind in _TIMEOUT_KINDS:
            value = getattr(timeouts_cfg, kind, None)
            if value is not None:
                timeouts[kind] = float(value)
        retry = getattr(http_cfg, "retry", None)
        return cls(
            user_agent=getattr(http_cfg, "user_agent", "makervox_publish"),
            timeouts=timeouts or {"default": 30.0},
            attempts=int(getattr(retry, "attempts", 3)),
            backoff_s=float(getattr(retry, "backoff_s", 3.0)),
            retry_on=tuple(getattr(retry, "retry_on", ("transport",))),
        )

    def timeout(self, kind: str = "default") -> float:
        return float(self.timeouts.get(kind, self.timeouts.get("default", 30.0)))

    def retries_status(self, status: Optional[int]) -> bool:
        """Whether an HTTP RESPONSE (not a transport failure) may be retried."""
        if status is None:
            return False
        if status == 429 and "429" in self.retry_on:
            return True
        return 500 <= status < 600 and "5xx" in self.retry_on


class HttpClient:
    """One ``requests.Session`` plus the policy above.

    A Session is used for connection reuse, not for state: nothing here depends
    on cookies, and every call carries its own Authorization header so one
    client can serve several accounts.
    """

    def __init__(self, policy: Optional[HttpPolicy] = None,
                 session: Optional[Any] = None) -> None:
        self.policy = policy or HttpPolicy(timeouts={"default": 30.0})
        self.session = session if session is not None else requests.Session()
        self.session.headers.setdefault("User-Agent", self.policy.user_agent)

    @classmethod
    def from_config(cls, http_cfg, session: Optional[Any] = None) -> "HttpClient":
        return cls(HttpPolicy.from_config(http_cfg), session=session)

    # -- the one request path ------------------------------------------------ #
    def request(
        self,
        method: str,
        url: str,
        *,
        kind: str = "default",
        attempts: Optional[int] = None,
        **kwargs: Any,
    ):
        """Perform a request, retrying ONLY what policy says is retryable."""
        kwargs.setdefault("timeout", self.policy.timeout(kind))
        budget = max(int(attempts if attempts is not None else self.policy.attempts), 1)
        last_exc = None  # type: Optional[BaseException]

        for attempt in range(1, budget + 1):
            try:
                response = self.session.request(method, url, **kwargs)
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                if attempt >= budget or "transport" not in self.policy.retry_on:
                    raise TransportError(method, url, attempt, exc)
                delay = self.policy.backoff_s * attempt
                log.warning(
                    "%s %s: transport error (%s), retry %d/%d in %.0fs",
                    method.upper(), _redact_url(url), type(exc).__name__,
                    attempt, budget - 1, delay,
                )
                time.sleep(delay)
                continue

            if attempt < budget and self.policy.retries_status(response.status_code):
                delay = self.policy.backoff_s * attempt
                log.warning(
                    "%s %s: HTTP %d, retry %d/%d in %.0fs (retry_on=%s)",
                    method.upper(), _redact_url(url), response.status_code,
                    attempt, budget - 1, delay, list(self.policy.retry_on),
                )
                time.sleep(delay)
                continue
            return response

        # Unreachable: the loop either returns or raises. Kept explicit so a
        # future edit cannot silently fall through to an implicit None, which is
        # the worst possible return value for a publish call.
        raise TransportError(method, url, budget, last_exc or RuntimeError("no attempt made"))

    def get(self, url: str, **kwargs: Any):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any):
        return self.request("PUT", url, **kwargs)

    # -- helpers ------------------------------------------------------------- #
    @staticmethod
    def json_body(response) -> Dict[str, Any]:
        """Parse a JSON body, tolerating an empty or non-JSON one.

        An API that answers 200 with an empty body (some upload endpoints do)
        must not turn into a ``JSONDecodeError`` three frames away from the call
        that would have explained it.
        """
        if not getattr(response, "content", None):
            return {}
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {"data": data}

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001 - closing must never fail a caller
            pass
