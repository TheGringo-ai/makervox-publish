"""Resolution for every ``impl`` key in the config file.

Anything spelled ``impl`` is a plugin: a ``"module.path:Callable"`` string plus
an ``options`` table passed to its constructor. That is how callables (UTM
taggers, cover pickers, post-identity parsers, state backends, credential
providers, media stagers) are injected from a config file, and it is why this
package can grow a Vault provider or an S3 stager without ever depending on
Vault or boto3.

Library callers who already hold real Python objects pass them to
``Config(...)`` directly and never touch this module.
"""

from __future__ import annotations

import importlib
from typing import Any, Mapping, Optional

from postvox.errors import PluginError

__all__ = ["resolve", "instantiate", "entry_point_names"]


def _entry_points(group: str):
    try:
        from importlib.metadata import entry_points as _eps
    except ImportError:  # pragma: no cover - Python < 3.8
        return []
    eps = _eps()
    selector = getattr(eps, "select", None)
    if selector is not None:  # Python >= 3.10
        return list(selector(group=group))
    return list(eps.get(group, []))  # type: ignore[union-attr]


def entry_point_names(group: str):
    """Names published under ``group``, for error messages and ``postvox doctor``."""
    return sorted(ep.name for ep in _entry_points(group))


def resolve(impl: Any, *, key: Optional[str] = None, group: Optional[str] = None) -> Any:
    """Turn an ``impl`` value into the object it names.

    Accepts, in order:
      * an already-usable Python object (class, function, instance) — returned
        as-is, which is what library callers pass;
      * ``"package.module:Attribute"`` — imported;
      * ``"package.module.Attribute"`` — imported (dotted fallback);
      * a bare name published under ``group`` as an entry point.
    """
    if impl is None:
        raise PluginError("missing 'impl'", key=key)
    if not isinstance(impl, str):
        return impl

    text = impl.strip()
    if not text:
        raise PluginError("empty 'impl'", key=key)

    if ":" in text:
        module_name, _, attr = text.partition(":")
        return _import_attr(module_name, attr, text, key)

    if group:
        for ep in _entry_points(group):
            if ep.name == text:
                try:
                    return ep.load()
                except Exception as exc:
                    raise PluginError(
                        "entry point {0!r} in group {1} failed to load: {2}".format(
                            text, group, exc
                        ),
                        key=key,
                    ) from exc

    if "." in text:
        module_name, _, attr = text.rpartition(".")
        return _import_attr(module_name, attr, text, key)

    known = entry_point_names(group) if group else []
    raise PluginError(
        "cannot resolve {0!r}; use 'module.path:ClassName'{1}".format(
            text,
            " or one of {0}".format(known) if known else "",
        ),
        key=key,
    )


def _import_attr(module_name: str, attr: str, original: str, key: Optional[str]) -> Any:
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise PluginError(
            "cannot import module {0!r} for impl {1!r}: {2}".format(
                module_name, original, exc
            ),
            key=key,
        ) from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise PluginError(
            "module {0!r} has no attribute {1!r} (from impl {2!r})".format(
                module_name, attr, original
            ),
            key=key,
        ) from exc


def instantiate(
    impl: Any,
    options: Optional[Mapping[str, Any]] = None,
    *,
    key: Optional[str] = None,
    group: Optional[str] = None,
) -> Any:
    """Resolve ``impl`` and call it with ``**options``.

    A non-callable resolution (someone pointed ``impl`` at a module-level
    instance) is returned as-is, but only when no options were supplied —
    otherwise the options would be silently dropped, which is exactly the class
    of silent misconfiguration this package refuses to ship.
    """
    target = resolve(impl, key=key, group=group)
    opts = dict(options or {})
    if not callable(target):
        if opts:
            raise PluginError(
                "impl {0!r} resolved to a non-callable {1}, so its 'options' "
                "would be ignored".format(impl, type(target).__name__),
                key=key,
            )
        return target
    try:
        return target(**opts)
    except TypeError as exc:
        raise PluginError(
            "cannot construct {0!r} with options {1}: {2}".format(
                impl, sorted(opts), exc
            ),
            key=key,
        ) from exc
