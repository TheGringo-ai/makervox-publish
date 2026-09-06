"""makervox_publish.config — one config surface, one place defaults live.

    from makervox_publish.config import Config

    cfg = Config.load()                       # searches for makervox-publish.toml
    cfg = Config.load("/etc/makervox-publish/makervox-publish.toml")
    cfg = Config.from_mapping({"platforms": {"x": {"enabled": True}}})
    cfg = Config.defaults()                   # everything default, no file

Three properties this module is built to guarantee:

* **Nothing is created on import or on load.** Directories are made on first
  WRITE only. Loading a config on a fresh machine touches no ``$HOME``.
* **Every validation error names its key.** ``ConfigError`` carries the dotted
  path and the file it came from, because "invalid config" with no key is how a
  self-hoster gets stuck.
* **Unknown keys are refused.** A typo that is silently ignored is how a safety
  setting ends up not applied while the file still looks right.
"""

from __future__ import annotations

from makervox_publish.config.loader import (
    CONFIG_ENV_VAR,
    ENV_OVERLAY_PREFIX,
    Cursor,
    deep_merge,
    env_overlay,
    expand_path,
    find_config_file,
    parse_file,
    search_paths,
)
from makervox_publish.config.plugins import instantiate, resolve
from makervox_publish.config.schema import (
    AccountConfig,
    AuthListenerConfig,
    CliConfig,
    Config,
    CoverConfig,
    CredentialsConfig,
    DedupeConfig,
    FacebookConfig,
    FingerprintConfig,
    HttpConfig,
    HttpTimeouts,
    IdentityConfig,
    InstagramConfig,
    LedgerConfig,
    LinksConfig,
    LocksConfig,
    LoggingConfig,
    MediaConfig,
    PlatformsConfig,
    PluginSpec,
    RetryConfig,
    ShortCutConfig,
    StateConfig,
    StripConfig,
    TikTokConfig,
    TranscodeConfig,
    XConfig,
)

__all__ = [
    "Config",
    "PluginSpec",
    "AccountConfig",
    "AuthListenerConfig",
    "CliConfig",
    "CoverConfig",
    "CredentialsConfig",
    "DedupeConfig",
    "FacebookConfig",
    "FingerprintConfig",
    "HttpConfig",
    "HttpTimeouts",
    "IdentityConfig",
    "InstagramConfig",
    "LedgerConfig",
    "LinksConfig",
    "LocksConfig",
    "LoggingConfig",
    "MediaConfig",
    "PlatformsConfig",
    "RetryConfig",
    "ShortCutConfig",
    "StateConfig",
    "StripConfig",
    "TikTokConfig",
    "TranscodeConfig",
    "XConfig",
    # loader helpers, used by the CLI and by tests
    "CONFIG_ENV_VAR",
    "ENV_OVERLAY_PREFIX",
    "Cursor",
    "deep_merge",
    "env_overlay",
    "expand_path",
    "find_config_file",
    "parse_file",
    "search_paths",
    # plugin resolution, used by every `impl` key
    "instantiate",
    "resolve",
]
