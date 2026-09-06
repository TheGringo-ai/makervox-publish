"""JsonFileProvider — a flat {"NAME": "value"} JSON file, or a .env file.

Refuses to load a file that is group- or world-readable unless
``allow_insecure_mode=True``: a secrets file at 0644 is a finding, not a
preference. ``required=False`` turns a missing file into a skip so the same
config works on a machine that does not have it.
"""

from __future__ import annotations

import json
import os
import stat
from typing import Dict, Optional

from postvox.credentials.base import ProviderUnavailable
from postvox.logging import get_logger

__all__ = ["JsonFileProvider"]

log = get_logger(__name__)

_INSECURE_BITS = stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH


class JsonFileProvider:
    def __init__(
        self,
        path,
        *,
        required: bool = True,
        allow_insecure_mode: bool = False,
    ) -> None:
        self.path = os.path.abspath(os.path.expanduser(str(path)))
        self.required = bool(required)
        self.allow_insecure_mode = bool(allow_insecure_mode)
        self.name = "file:{0}".format(self.path)
        self._values = self._load()

    # -- loading ----------------------------------------------------------- #
    def _load(self) -> Dict[str, str]:
        if not os.path.exists(self.path):
            if self.required:
                raise ProviderUnavailable(
                    "credential file {0!r} does not exist. Create it, or set "
                    "required = false on this provider.".format(self.path)
                )
            log.warning("credential file %s is absent; provider skipped", self.path)
            return {}

        self._check_mode()

        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            raise ProviderUnavailable(
                "cannot read credential file {0!r}: {1}".format(self.path, exc)
            )

        text = raw.strip()
        if text.startswith("{"):
            return self._parse_json(text)
        return self._parse_dotenv(raw)

    def _check_mode(self) -> None:
        try:
            mode = os.stat(self.path).st_mode
        except OSError:  # pragma: no cover - raced away between exists() and stat()
            return
        if not (mode & _INSECURE_BITS):
            return
        if self.allow_insecure_mode:
            log.warning(
                "credential file %s is mode %o (group/world readable); loading "
                "anyway because allow_insecure_mode is set",
                self.path, stat.S_IMODE(mode),
            )
            return
        raise ProviderUnavailable(
            "credential file {0!r} is mode {1:o} — group- or world-readable. "
            "Run `chmod 600 {0}`, or set allow_insecure_mode = true if you "
            "really mean it.".format(self.path, stat.S_IMODE(mode))
        )

    def _parse_json(self, text: str) -> Dict[str, str]:
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ProviderUnavailable(
                "credential file {0!r} is not valid JSON: {1}".format(self.path, exc)
            )
        if not isinstance(data, dict):
            raise ProviderUnavailable(
                "credential file {0!r} must hold a flat object of "
                "name -> value".format(self.path)
            )
        out = {}  # type: Dict[str, str]
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                raise ProviderUnavailable(
                    "credential file {0!r} key {1!r} is nested; this provider "
                    "reads a FLAT name -> value mapping".format(self.path, key)
                )
            if value is None:
                continue
            out[str(key)] = str(value)
        return out

    def _parse_dotenv(self, raw: str) -> Dict[str, str]:
        out = {}  # type: Dict[str, str]
        for lineno, line in enumerate(raw.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                log.warning("%s:%d: ignoring line without '='", self.path, lineno)
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key:
                out[key] = value
        return out

    # -- provider interface ------------------------------------------------ #
    def get(self, key: str) -> Optional[str]:
        return (self._values.get(key) or "").strip() or None

    def has(self, key: str) -> bool:
        return bool((self._values.get(key) or "").strip())

    def describe(self) -> str:
        return "file {0} ({1} keys)".format(self.path, len(self._values))

    def __repr__(self) -> str:
        return "<JsonFileProvider {0} ({1} keys)>".format(self.path, len(self._values))
