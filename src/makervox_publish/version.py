"""Single source of truth for the package version."""

from __future__ import annotations

__all__ = ["__version__", "VERSION", "user_agent"]

__version__ = "0.1.0"
VERSION = __version__

#: The config schema version this build understands. Bumps are breaking.
CONFIG_SCHEMA_VERSION = 1

#: The delivery-ledger row schema version. Frozen; new fields are additive only.
LEDGER_SCHEMA_VERSION = 2


def user_agent(project_url: str = "https://github.com/example-org/makervox_publish") -> str:
    """Default User-Agent string. Overridable via ``http.user_agent`` in config."""
    return "makervox_publish/{v} (+{url})".format(v=__version__, url=project_url)
