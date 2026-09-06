"""makervox_publish — self-hosted social publishing for people who bring their own apps.

This package assumes ONE self-hoster with THEIR OWN developer app on each
platform: your own TikTok app, your own Meta app, your own X app. There is no
shared app, no hosted OAuth broker and no default that points at anyone else's
domain or cloud project. Anything only you can supply — an OAuth redirect URI, a
staging bucket, a cloud project id — has NO default and raises
:class:`~makervox_publish.errors.ConfigError` if something needs it.

Quick start, with no config file at all::

    from makervox_publish import Config, Credentials

    cfg = Config.defaults()             # every default; nothing written to disk
    creds = Credentials.default()       # environment variables only
    values, missing = creds.require(["X_API_KEY", "X_API_SECRET"])

Importing this module touches no socket, no subprocess and no ``$HOME``.
Platform clients are NOT imported here: they live under ``makervox_publish.platforms``
and are imported explicitly by the caller (or by the CLI), which keeps the
layering strictly one-directional::

    config / credentials / state / media / text / identity / http
        <- platforms
            <- cli
"""

from __future__ import annotations

from makervox_publish.config import Config, PluginSpec
from makervox_publish.credentials import (
    ChainProvider,
    CredentialError,
    CredentialNotFound,
    Credentials,
    CredentialSpec,
    EnvProvider,
    ProviderUnavailable,
    StaticProvider,
)
from makervox_publish.errors import (
    ConfigError,
    DuplicatePost,
    FfmpegFailed,
    FfmpegNotFound,
    IdentityClash,
    LockUnavailable,
    MediaError,
    NotConfigured,
    PluginError,
    MakervoxPublishError,
    PublishError,
    ReauthorizationRequired,
    StagingPreconditionError,
    TokenRotationError,
)
from makervox_publish.logging import configure_logging, get_logger, mask, register_secret
from makervox_publish.media import FfmpegTools, cover_offset_ms
from makervox_publish.version import __version__

__all__ = [
    "__version__",
    # config
    "Config",
    "PluginSpec",
    # credentials
    "Credentials",
    "CredentialSpec",
    "CredentialProviderChain",
    "ChainProvider",
    "EnvProvider",
    "StaticProvider",
    "CredentialError",
    "CredentialNotFound",
    "ProviderUnavailable",
    # media helpers shared by every platform
    "FfmpegTools",
    "cover_offset_ms",
    # logging
    "get_logger",
    "configure_logging",
    "register_secret",
    "mask",
    # errors
    "MakervoxPublishError",
    "ConfigError",
    "PluginError",
    "MediaError",
    "FfmpegNotFound",
    "FfmpegFailed",
    "PublishError",
    "NotConfigured",
    "DuplicatePost",
    "StagingPreconditionError",
    "TokenRotationError",
    "ReauthorizationRequired",
    "IdentityClash",
    "LockUnavailable",
]

#: Readable alias — the chain IS the provider list, and callers say so.
CredentialProviderChain = ChainProvider
