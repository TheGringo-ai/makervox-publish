"""Finding, parsing and merging config files, plus typed access helpers.

Search order (first file wins), overridable with ``MAKERVOX_PUBLISH_CONFIG=/path/to.toml``:

    ./makervox-publish.toml
    $XDG_CONFIG_HOME/makervox_publish/makervox-publish.toml   (~/.config/makervox-publish/makervox-publish.toml)
    /etc/makervox-publish/makervox-publish.toml

TOML is the shipped format (stdlib ``tomllib`` on 3.11+, the ``tomli`` extra
below that). JSON is always available. YAML is read when PyYAML happens to be
installed, and only then — the package's single runtime dependency is requests.

Defaults live in exactly one place: the dataclasses in ``schema.py``. There is
no shipped defaults file to drift out of sync with them.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from makervox_publish.errors import ConfigError

__all__ = [
    "CONFIG_ENV_VAR",
    "ENV_OVERLAY_PREFIX",
    "search_paths",
    "find_config_file",
    "parse_file",
    "deep_merge",
    "env_overlay",
    "expand_path",
    "Cursor",
]

CONFIG_ENV_VAR = "MAKERVOX_PUBLISH_CONFIG"
ENV_OVERLAY_PREFIX = "MAKERVOX_PUBLISH_"
_BASENAMES = ("makervox-publish.toml", "makervox-publish.yaml", "makervox-publish.yml", "makervox-publish.json")


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def expand_path(value: str) -> str:
    """``~`` and ``$VAR`` expansion. Never creates anything."""
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(value))))


def _xdg_config_home() -> str:
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")


def search_paths(cwd: Optional[str] = None) -> List[str]:
    """The ordered candidate list, honouring ``MAKERVOX_PUBLISH_CONFIG``."""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return [expand_path(override)]

    here = cwd or os.getcwd()
    out = []  # type: List[str]
    for base in _BASENAMES:
        out.append(os.path.join(here, base))
    for base in _BASENAMES:
        out.append(os.path.join(_xdg_config_home(), "makervox_publish", base))
    for base in _BASENAMES:
        out.append(os.path.join("/etc", "makervox_publish", base))
    return out


def find_config_file(cwd: Optional[str] = None) -> Optional[str]:
    """First existing candidate, or None. Reads no file and creates nothing."""
    for candidate in search_paths(cwd):
        if os.path.isfile(candidate):
            return candidate
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        raise ConfigError(
            "{0} points at {1!r}, which does not exist".format(CONFIG_ENV_VAR, override),
            source=override,
        )
    return None


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _load_toml(raw: bytes, path: str) -> Dict[str, Any]:
    try:
        import tomllib as toml_mod  # Python >= 3.11
    except ImportError:
        try:
            import tomli as toml_mod  # type: ignore[no-redef]
        except ImportError as exc:
            raise ConfigError(
                "reading TOML on Python < 3.11 needs the tomli extra: "
                "pip install 'makervox-publish[toml]' (or use a .json config)",
                source=path,
            ) from exc
    try:
        return dict(toml_mod.loads(raw.decode("utf-8")))
    except Exception as exc:
        raise ConfigError("invalid TOML: {0}".format(exc), source=path) from exc


def _load_yaml(raw: bytes, path: str) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ConfigError(
            "reading YAML needs the yaml extra: pip install 'makervox-publish[yaml]' "
            "(TOML and JSON need nothing extra)",
            source=path,
        ) from exc
    try:
        data = yaml.safe_load(raw.decode("utf-8")) or {}
    except Exception as exc:
        raise ConfigError("invalid YAML: {0}".format(exc), source=path) from exc
    if not isinstance(data, Mapping):
        raise ConfigError("top level must be a mapping", source=path)
    return dict(data)


def _load_json(raw: bytes, path: str) -> Dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ConfigError("invalid JSON: {0}".format(exc), source=path) from exc
    if not isinstance(data, Mapping):
        raise ConfigError("top level must be a mapping", source=path)
    return dict(data)


def parse_file(path: str) -> Dict[str, Any]:
    """Parse one config file by extension. Raises ConfigError naming the file."""
    full = expand_path(path)
    try:
        with open(full, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ConfigError("cannot read config file: {0}".format(exc), source=full) from exc

    ext = os.path.splitext(full)[1].lower()
    if ext == ".toml":
        return _load_toml(raw, full)
    if ext in (".yaml", ".yml"):
        return _load_yaml(raw, full)
    if ext == ".json":
        return _load_json(raw, full)
    raise ConfigError(
        "unknown config extension {0!r}; use .toml, .json or .yaml".format(ext),
        source=full,
    )


# --------------------------------------------------------------------------- #
# merging + env overlay
# --------------------------------------------------------------------------- #
def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursive dict merge; lists are REPLACED, never concatenated.

    Concatenating lists would make it impossible to shorten an allow-list from
    an overlay, and a silently-lengthened allow-list is a security bug.
    """
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce_env_value(text: str) -> Any:
    lowered = text.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none", ""):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def env_overlay(environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Build an overlay mapping from ``MAKERVOX_PUBLISH_``-prefixed environment variables.

    ``__`` separates path segments and the remainder is lowercased::

        MAKERVOX_PUBLISH_PLATFORMS__X__GOVERNOR__DAILY_CAP=2
            -> {"platforms": {"x": {"governor": {"daily_cap": 2}}}}

    Values are parsed as JSON when possible, so ``[]``, ``true`` and ``3``
    arrive as the right type. ``MAKERVOX_PUBLISH_CONFIG`` is excluded: it selects the
    file, it is not a key inside it.
    """
    env = environ if environ is not None else os.environ
    out = {}  # type: Dict[str, Any]
    for name, value in env.items():
        if not name.startswith(ENV_OVERLAY_PREFIX) or name == CONFIG_ENV_VAR:
            continue
        trail = name[len(ENV_OVERLAY_PREFIX):]
        if not trail:
            continue
        parts = [p.lower() for p in trail.split("__") if p]
        if not parts:
            continue
        node = out
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = _coerce_env_value(value)
    return out


# --------------------------------------------------------------------------- #
# typed access — every failure names the key path
# --------------------------------------------------------------------------- #
_MISSING = object()


class Cursor:
    """A mapping plus the dotted key path that led to it.

    Every getter validates the type and raises :class:`ConfigError` naming the
    exact key, because "invalid config" with no key is the most common way a
    self-hoster gets stuck.
    """

    __slots__ = ("data", "path", "source")

    def __init__(self, data: Optional[Mapping[str, Any]] = None, path: str = "",
                 source: Optional[str] = None) -> None:
        if data is None:
            data = {}
        if not isinstance(data, Mapping):
            raise ConfigError("expected a table/mapping", key=path or "<root>", source=source)
        self.data = data
        self.path = path
        self.source = source

    # -- navigation --------------------------------------------------------- #
    def _key(self, key: str) -> str:
        return "{0}.{1}".format(self.path, key) if self.path else key

    def child(self, key: str) -> "Cursor":
        """Sub-table. A missing table is an EMPTY one, so defaults apply."""
        value = self.data.get(key)
        if value is None:
            return Cursor({}, self._key(key), self.source)
        if not isinstance(value, Mapping):
            raise ConfigError(
                "expected a table, got {0}".format(type(value).__name__),
                key=self._key(key), source=self.source,
            )
        return Cursor(value, self._key(key), self.source)

    def children(self) -> Sequence[Tuple[str, "Cursor"]]:
        """Every sub-table, for open-ended blocks like ``accounts``."""
        out = []
        for key, value in self.data.items():
            if not isinstance(value, Mapping):
                raise ConfigError(
                    "expected a table, got {0}".format(type(value).__name__),
                    key=self._key(str(key)), source=self.source,
                )
            out.append((str(key), Cursor(value, self._key(str(key)), self.source)))
        return out

    def present(self, key: str) -> bool:
        return key in self.data and self.data[key] is not None

    # -- scalars ------------------------------------------------------------ #
    def _get(self, key: str, default: Any, required: bool) -> Any:
        value = self.data.get(key, _MISSING)
        if value is _MISSING or value is None:
            if required:
                raise ConfigError(
                    "required value is missing. This one has no default because "
                    "only you can supply it.",
                    key=self._key(key), source=self.source,
                )
            return default
        return value

    def str(self, key: str, default: Optional[str] = None, *, required: bool = False) -> Optional[str]:
        value = self._get(key, default, required)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ConfigError(
                "expected a string, got {0}".format(type(value).__name__),
                key=self._key(key), source=self.source,
            )
        return str(value)

    def path_str(self, key: str, default: Optional[str] = None, *,
                 required: bool = False) -> Optional[str]:
        """A string that is a filesystem path: expanded, never created."""
        value = self.str(key, default, required=required)
        return expand_path(value) if value else None

    def int(self, key: str, default: Optional[int] = None, *,
            required: bool = False, minimum: Optional[int] = None,
            maximum: Optional[int] = None) -> Optional[int]:
        value = self._get(key, default, required)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ConfigError(
                "expected an integer, got {0}".format(type(value).__name__),
                key=self._key(key), source=self.source,
            )
        try:
            out = int(value, 0) if isinstance(value, str) else int(value)
        except ValueError as exc:
            raise ConfigError(
                "expected an integer: {0}".format(exc),
                key=self._key(key), source=self.source,
            ) from exc
        if minimum is not None and out < minimum:
            raise ConfigError("must be >= {0} (got {1})".format(minimum, out),
                              key=self._key(key), source=self.source)
        if maximum is not None and out > maximum:
            raise ConfigError("must be <= {0} (got {1})".format(maximum, out),
                              key=self._key(key), source=self.source)
        return out

    def float(self, key: str, default: Optional[float] = None, *,
              required: bool = False, minimum: Optional[float] = None) -> Optional[float]:
        value = self._get(key, default, required)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ConfigError(
                "expected a number, got {0}".format(type(value).__name__),
                key=self._key(key), source=self.source,
            )
        try:
            out = float(value)
        except ValueError as exc:
            raise ConfigError("expected a number: {0}".format(exc),
                              key=self._key(key), source=self.source) from exc
        if minimum is not None and out < minimum:
            raise ConfigError("must be >= {0} (got {1})".format(minimum, out),
                              key=self._key(key), source=self.source)
        return out

    def bool(self, key: str, default: Optional[bool] = None, *,
             required: bool = False) -> Optional[bool]:
        value = self._get(key, default, required)
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no",
                                                               "1", "0", "on", "off"):
            return value.strip().lower() in ("true", "yes", "1", "on")
        raise ConfigError(
            "expected true or false, got {0!r}".format(value),
            key=self._key(key), source=self.source,
        )

    def choice(self, key: str, allowed: Sequence[str], default: Optional[str] = None, *,
               required: bool = False) -> Optional[str]:
        value = self.str(key, default, required=required)
        if value is None:
            return None
        if value not in allowed:
            raise ConfigError(
                "must be one of {0} (got {1!r})".format(list(allowed), value),
                key=self._key(key), source=self.source,
            )
        return value

    # -- collections -------------------------------------------------------- #
    def str_list(self, key: str, default: Sequence[str] = ()) -> Tuple[str, ...]:
        value = self.data.get(key)
        if value is None:
            return tuple(default)
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ConfigError(
                "expected a list of strings, got {0}".format(type(value).__name__),
                key=self._key(key), source=self.source,
            )
        out = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ConfigError(
                    "expected a string, got {0}".format(type(item).__name__),
                    key="{0}[{1}]".format(self._key(key), index), source=self.source,
                )
            out.append(item)
        return tuple(out)

    def int_list(self, key: str, default: Sequence[int] = ()) -> Tuple[int, ...]:
        value = self.data.get(key)
        if value is None:
            return tuple(default)
        if not isinstance(value, (list, tuple)):
            raise ConfigError(
                "expected a list of integers, got {0}".format(type(value).__name__),
                key=self._key(key), source=self.source,
            )
        out = []
        for index, item in enumerate(value):
            if isinstance(item, bool) or not isinstance(item, int):
                raise ConfigError(
                    "expected an integer, got {0}".format(type(item).__name__),
                    key="{0}[{1}]".format(self._key(key), index), source=self.source,
                )
            out.append(int(item))
        return tuple(out)

    def table_list(self, key: str) -> List["Cursor"]:
        """A list of tables (``[[credentials.providers]]``)."""
        value = self.data.get(key)
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ConfigError(
                "expected a list of tables, got {0}".format(type(value).__name__),
                key=self._key(key), source=self.source,
            )
        out = []
        for index, item in enumerate(value):
            item_key = "{0}[{1}]".format(self._key(key), index)
            if not isinstance(item, Mapping):
                raise ConfigError(
                    "expected a table, got {0}".format(type(item).__name__),
                    key=item_key, source=self.source,
                )
            out.append(Cursor(item, item_key, self.source))
        return out

    def mapping(self, key: str) -> Dict[str, Any]:
        """A free-form table handed straight to a plugin constructor."""
        value = self.data.get(key)
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ConfigError(
                "expected a table, got {0}".format(type(value).__name__),
                key=self._key(key), source=self.source,
            )
        return dict(value)

    # -- validation --------------------------------------------------------- #
    def reject_unknown(self, allowed: Sequence[str]) -> None:
        """Fail on a key we do not understand.

        A typo that is silently ignored is how a safety setting ends up not
        applied while the config file still *looks* right.
        """
        unknown = [k for k in self.data.keys() if k not in allowed]
        if unknown:
            raise ConfigError(
                "unknown key(s) {0}; known keys here are {1}".format(
                    sorted(unknown), sorted(allowed)
                ),
                key=self.path or "<root>", source=self.source,
            )

    def __repr__(self) -> str:
        return "<Cursor {0!r} keys={1}>".format(self.path or "<root>", sorted(self.data))
