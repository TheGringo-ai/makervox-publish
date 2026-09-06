"""How a TikTok API response is turned into an actionable exception.

The envelope is always ``{"data": {...}, "error": {"code": "ok"|"...",
"message": "...", "log_id": "..."}}``. ``code == "ok"`` (and, on some
endpoints, an absent error block) means success — checking only the HTTP status
is not enough, because a 200 can carry a rejection.

``log_id`` is carried through into the message on purpose: it is the only handle
support has on a specific call, and it is gone by the time anyone thinks to ask
for it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from postvox.errors import PublishError

__all__ = ["TikTokApiError", "api_error", "is_ok", "is_scope_error",
           "raise_for_api_error"]


class TikTokApiError(PublishError):
    """A TikTok API call returned an error envelope."""

    def __init__(self, what: str, *, payload: Optional[Dict[str, Any]] = None,
                 account: str = "", hint: str = "") -> None:
        err = api_error(payload or {})
        code = err.get("code") or (payload or {}).get("error") or "unknown"
        message = err.get("message") or (payload or {}).get("error_description") or ""
        log_id = err.get("log_id") or ""
        parts = ["{0}: {1}".format(what, code)]
        if message:
            parts.append(message)
        if account:
            parts.append("account={0!r}".format(account))
        if log_id:
            parts.append("log_id={0}".format(log_id))
        if hint:
            parts.append("-> " + hint)
        super().__init__(" | ".join(parts))
        self.code = code
        self.api_message = message
        self.log_id = log_id
        self.account = account
        self.payload = payload or {}


def api_error(payload: Dict[str, Any]) -> Dict[str, Any]:
    err = payload.get("error")
    return err if isinstance(err, dict) else {}


def is_ok(payload: Dict[str, Any]) -> bool:
    """True when the envelope reports success.

    An ABSENT error block counts as success: several endpoints omit it entirely
    on the happy path, and treating that as a failure breaks every one of them.
    """
    code = api_error(payload).get("code")
    return code in (None, "", "ok")


def is_scope_error(payload: Dict[str, Any]) -> bool:
    """Whether this rejection is about a missing scope.

    Matched on the code AND the message, because the same condition surfaces as
    ``scope_not_authorized`` on some endpoints and as a plain
    ``access_token_invalid`` with a scope sentence on others.
    """
    err = api_error(payload)
    haystack = "{0} {1} {2}".format(
        err.get("code") or "", err.get("message") or "",
        payload.get("error") or "",
    ).lower()
    return "scope" in haystack


def raise_for_api_error(payload: Dict[str, Any], what: str, *, account: str = "",
                        hint: str = "") -> Dict[str, Any]:
    """Return ``payload["data"]`` or raise. The one gate every call goes through."""
    if not is_ok(payload):
        raise TikTokApiError(what, payload=payload, account=account, hint=hint)
    data = payload.get("data")
    return data if isinstance(data, dict) else {}
