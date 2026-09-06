"""postvox — self-hosted social publishing for people who bring their own apps.

This package assumes ONE self-hoster with THEIR OWN developer app on each
platform: your own TikTok app, your own Meta app, your own X app. There is no
shared app, no hosted OAuth broker and no default that points at anyone else's
domain or cloud project. Anything only you can supply — an OAuth redirect URI, a
staging bucket, a cloud project id — has NO default and raises
:class:`~postvox.errors.ConfigError` if something needs it.

Quick start, with no config file at all::

    from postvox import Config, Credentials

    cfg = Config.defaults()             # every default; nothing written to disk
    creds = Credentials.default()       # environment variables only
    values, missing = creds.require(["X_API_KEY", "X_API_SECRET"])

Importing this module touches no socket, no subprocess and no ``$HOME``.
Platform clients are NOT imported here: they live under ``postvox.platforms``
and are imported explicitly by the caller (or by the CLI), which keeps the
layering strictly one-directional::

    config / credentials / state / media / text / identity / http
        <- platforms
            <- cli
"""

from __future__ import annotations

from postvox.config import Config, PluginSpec
from postvox.credentials import (
    ChainProvider,
    CredentialError,
    CredentialNotFound,
    Credentials,
    CredentialSpec,
    EnvProvider,
    ProviderUnavailable,
    StaticProvider,
)
from postvox.errors import (
    ConfigError,
    DuplicatePost,
    FfmpegFailed,
    FfmpegNotFound,
    IdentityClash,
    LockUnavailable,
    MediaError,
    NotConfigured,
    PluginError,
    PostvoxError,
    PublishError,
    ReauthorizationRequired,
    StagingPreconditionError,
    TokenRotationError,
)
from postvox.logging import configure_logging, get_logger, mask, register_secret
from postvox.media import FfmpegTools, cover_offset_ms
from postvox.version import __version__

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
    "PostvoxError",
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
