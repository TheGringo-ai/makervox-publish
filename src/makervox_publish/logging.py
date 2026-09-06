"""Logger tree + a secret-redaction filter.

This package NEVER print()s. Two rules follow from that:

1. Every deliberate exception swallow (cover art, thumbnail, transcode
   fallback, staged-object cleanup, insights) logs at
   ``logging.swallowed_error_level`` WITH the reason. A silent swallow is how
   an ffmpeg failure turns into the exact upload rejection the transcode
   existed to prevent.
2. Resolved secrets are registered with :func:`register_secret` and scrubbed
   from every record emitted under the ``makervox_publish`` logger tree, so an
   accidental ``log.debug("headers=%s", headers)`` cannot leak a token.

Library default: we do not touch root logging. An application opts in with
``configure(...)`` or with its own handlers.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable, Optional

__all__ = [
    "ROOT_LOGGER_NAME",
    "get_logger",
    "configure",
    "configure_logging",
    "configure_from_config",
    "register_secret",
    "forget_secrets",
    "mask",
    "SecretRedactionFilter",
    "swallowed",
]

ROOT_LOGGER_NAME = "makervox_publish"

#: Minimum length before a value is worth redacting. Short values ("1", "on")
#: would otherwise scrub unrelated text into uselessness.
_MIN_SECRET_LEN = 6

_lock = threading.Lock()
_secrets = set()  # type: set

#: Every logger this module has handed out, so a later ``configure(redact=...)``
#: can attach or detach the filter on all of them. A filter attached to the
#: PARENT logger does not see records logged through a CHILD — records only pass
#: the filters of the logger they were logged on, plus each handler's — so the
#: filter goes on every logger we create, not just the root of the tree.
_known_loggers = []  # type: list

_redaction_enabled = True

#: Level used by :func:`swallowed` when a caller does not name one. Set from
#: ``logging.swallowed_error_level`` by :func:`configure`, so every deliberate
#: swallow in the package moves together.
_swallow_level = "WARNING"


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a child of the ``makervox_publish`` logger.

    ``get_logger(__name__)`` inside the package returns that module's logger
    unchanged; anything else is attached under the package root.
    """
    if not name or name == ROOT_LOGGER_NAME:
        resolved = ROOT_LOGGER_NAME
    elif name.startswith(ROOT_LOGGER_NAME + "."):
        resolved = name
    else:
        resolved = "{0}.{1}".format(ROOT_LOGGER_NAME, name)
    log = logging.getLogger(resolved)
    _register_logger(log)
    return log


def _register_logger(log: logging.Logger) -> None:
    with _lock:
        if log not in _known_loggers:
            _known_loggers.append(log)
        enabled = _redaction_enabled
    if enabled and _redaction_filter not in log.filters:
        log.addFilter(_redaction_filter)


def register_secret(value: Optional[str]) -> None:
    """Mark a resolved secret so it is scrubbed from every makervox_publish log record."""
    if not value or len(value) < _MIN_SECRET_LEN:
        return
    with _lock:
        _secrets.add(value)


def forget_secrets() -> None:
    """Drop every registered secret (tests, credential rotation)."""
    with _lock:
        _secrets.clear()


def mask(value: Optional[str], keep: int = 4) -> str:
    """Render a secret for humans: ``"abcd…(len=40)"``. Never the whole value."""
    if not value:
        return "<unset>"
    head = value[:keep]
    return "{0}…(len={1})".format(head, len(value))


def _scrub(text: str) -> str:
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, "***REDACTED***")
    return text


def _scrub_arg(arg):
    """Scrub a log argument of any type.

    Non-string arguments are stringified only to CHECK them; a clean argument is
    returned untouched so formatting (``%d``, ``%.2f``) still works. An
    exception object whose message embeds a token is the case that matters —
    ``log.warning("failed: %s", exc)`` would otherwise print it in full.
    """
    if isinstance(arg, str):
        return _scrub(arg)
    try:
        text = str(arg)
    except Exception:  # pragma: no cover - a __str__ that raises
        return arg
    scrubbed = _scrub(text)
    return scrubbed if scrubbed != text else arg


class SecretRedactionFilter(logging.Filter):
    """Replaces registered secret values anywhere in a record's text.

    Attached to the ``makervox_publish`` logger by :func:`configure`. Filters on a
    logger run for records logged through that logger (and its children), which
    is exactly the scope we own.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - stdlib name
        with _lock:
            if not _secrets:
                return True
        try:
            if isinstance(record.msg, str):
                record.msg = _scrub(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: _scrub_arg(v) for k, v in record.args.items()}
                elif isinstance(record.args, tuple):
                    record.args = tuple(_scrub_arg(a) for a in record.args)
            if record.exc_info and not record.exc_text:
                # Format the traceback HERE so it can be scrubbed. A Formatter
                # would otherwise render it after every filter has run, and an
                # exception carrying a token in its message would go out intact.
                record.exc_text = _scrub(
                    logging.Formatter().formatException(record.exc_info)
                )
        except Exception:  # pragma: no cover - logging must never break a publish
            return True
        return True


_redaction_filter = SecretRedactionFilter()


def _set_redaction(enabled: bool) -> None:
    global _redaction_enabled
    with _lock:
        _redaction_enabled = bool(enabled)
        loggers = list(_known_loggers)
    for log in loggers:
        if enabled and _redaction_filter not in log.filters:
            log.addFilter(_redaction_filter)
        elif not enabled and _redaction_filter in log.filters:
            log.removeFilter(_redaction_filter)


def configure(
    level: str = "INFO",
    *,
    logger_name: str = ROOT_LOGGER_NAME,
    redact: bool = True,
    configure_root: bool = False,
    handler: Optional[logging.Handler] = None,
    swallowed_error_level: Optional[str] = None,
) -> logging.Logger:
    """Attach the redaction filter and (optionally) a handler.

    Idempotent: calling it twice does not double-log. ``configure_root`` stays
    False by default because a library that reconfigures root logging is a
    library that fights its host application.
    """
    global _swallow_level

    log = logging.getLogger(logger_name)
    log.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if swallowed_error_level:
        _swallow_level = str(swallowed_error_level).upper()

    _set_redaction(redact)
    _register_logger(log)
    if handler is not None and redact and _redaction_filter not in handler.filters:
        # Also on the handler, so records from a caller's own logger that end up
        # here are scrubbed too.
        handler.addFilter(_redaction_filter)

    if handler is not None:
        if not any(getattr(h, "_makervox_publish_handler", False) for h in log.handlers):
            handler._makervox_publish_handler = True  # type: ignore[attr-defined]
            log.addHandler(handler)
    elif not log.handlers and not configure_root:
        # A library with no handler emits "no handlers could be found" noise on
        # some hosts; a NullHandler is the documented fix.
        log.addHandler(logging.NullHandler())

    if configure_root:
        logging.basicConfig(level=log.level)
    log.propagate = bool(configure_root) or log.propagate
    return log


def configure_from_config(logging_cfg, *, redact: bool = True,
                          handler: Optional[logging.Handler] = None) -> logging.Logger:
    """Apply a :class:`makervox_publish.config.LoggingConfig`."""
    return configure(
        getattr(logging_cfg, "level", "INFO"),
        logger_name=getattr(logging_cfg, "logger_name", ROOT_LOGGER_NAME),
        redact=redact,
        configure_root=bool(getattr(logging_cfg, "configure_root", False)),
        handler=handler,
        swallowed_error_level=getattr(logging_cfg, "swallowed_error_level", None),
    )


def swallowed(
    log: logging.Logger,
    what: str,
    exc: BaseException,
    *,
    level: Optional[str] = None,
    detail: str = "",
) -> None:
    """Record a deliberate, non-fatal failure.

    Use this at every ``except`` that intentionally continues. A cover frame is
    never worth failing a post over — but a cover frame that silently vanished
    is how you end up with a wall of black thumbnails and no idea why.
    """
    numeric = getattr(logging, str(level or _swallow_level).upper(), logging.WARNING)
    log.log(
        numeric,
        "%s failed (continuing): %s: %s%s",
        what,
        type(exc).__name__,
        exc,
        " | " + detail if detail else "",
    )


def levels() -> Iterable[str]:
    """Level names accepted in config, for validation error messages."""
    return ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET")


#: Explicit alias for callers who import it at the package top level, where a
#: bare ``configure`` would read as "configure what?".
configure_logging = configure
