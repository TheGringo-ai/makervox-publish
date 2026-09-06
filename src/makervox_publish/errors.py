"""Every exception makervox_publish raises on purpose.

Layering rule: this module imports nothing from makervox_publish, so any module may
import it. Errors carry the *actionable* detail in their message — a raw API
string like "scope not authorized" does not tell a self-hoster that the fix is
a RE-AUTHORIZATION and not a retry, so we say it here.
"""

from __future__ import annotations

from typing import Optional, Sequence

__all__ = [
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


class MakervoxPublishError(Exception):
    """Base class for everything this package raises deliberately."""


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
class ConfigError(MakervoxPublishError):
    """A config value is missing, of the wrong type, or self-contradictory.

    Always names the offending key path, because "invalid config" without a key
    is the single most common way a self-hoster gets stuck.
    """

    def __init__(self, message: str, *, key: Optional[str] = None,
                 source: Optional[str] = None) -> None:
        where = ""
        if key:
            where += " at {0!r}".format(key)
        if source:
            where += " (in {0})".format(source)
        super().__init__("config error{0}: {1}".format(where, message))
        self.key = key
        self.source = source


class PluginError(ConfigError):
    """An ``impl:`` string could not be imported or is not callable."""


# --------------------------------------------------------------------------- #
# media
# --------------------------------------------------------------------------- #
class MediaError(MakervoxPublishError):
    """Something went wrong inspecting or re-encoding a media file."""


class FfmpegNotFound(MediaError):
    """ffmpeg/ffprobe is not on PATH and no absolute path was configured."""

    def __init__(self, binary: str, configured: str) -> None:
        super().__init__(
            "{0} not found (configured as {1!r}). Install ffmpeg, or set "
            "media.{0}_path to an absolute path, or set media.require_ffmpeg "
            "false to disable the features that need it.".format(binary, configured)
        )
        self.binary = binary
        self.configured = configured


class FfmpegFailed(MediaError):
    """An ffmpeg/ffprobe invocation exited non-zero or timed out."""

    def __init__(self, argv: Sequence[str], returncode: Optional[int],
                 stderr_tail: str = "", timed_out: bool = False) -> None:
        how = "timed out" if timed_out else "exited {0}".format(returncode)
        super().__init__(
            "{0} {1}{2}".format(
                argv[0] if argv else "ffmpeg",
                how,
                ": " + stderr_tail.strip() if stderr_tail.strip() else "",
            )
        )
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        self.timed_out = timed_out


# --------------------------------------------------------------------------- #
# publishing
# --------------------------------------------------------------------------- #
class PublishError(MakervoxPublishError):
    """A publish attempt failed for a reason the caller cannot fix by retrying."""


class NotConfigured(MakervoxPublishError):
    """A platform was asked to publish before its credentials were supplied.

    Publishers normally return ``(False, reason)`` instead of raising this, so
    that one unconfigured platform never aborts a batch containing three
    working ones. It exists for callers that explicitly ask for strictness.
    """

    def __init__(self, platform: str, missing: Sequence[str], setup: str = "") -> None:
        super().__init__(
            "{0} is not configured: missing {1}.{2}".format(
                platform, ", ".join(missing) or "credentials",
                " " + setup if setup else "",
            )
        )
        self.platform = platform
        self.missing = list(missing)
        self.setup = setup


class DuplicatePost(MakervoxPublishError):
    """This exact content was already delivered for this account/date/key.

    Note that the normal publish path does NOT raise this: a detected duplicate
    returns ok=True, because the post IS live, which is what the caller was
    actually asking for.
    """


class StagingPreconditionError(MakervoxPublishError):
    """The staging bucket/host cannot serve a publicly fetchable URL.

    The common case: uniform bucket-level access is ENABLED (the default on
    modern GCS buckets), so per-object public ACLs raise. Surfacing that as a
    named error beats a raw 403 that reads like an auth problem.
    """


class TokenRotationError(MakervoxPublishError):
    """A refresh token was spent and the new one could not be persisted.

    This is the unrecoverable case: the old refresh token is already dead. The
    message must tell the operator to re-authorize rather than retry.
    """

    def __init__(self, platform: str, account: str, detail: str = "") -> None:
        super().__init__(
            "{0}/{1}: refresh token was rotated but the new token could not be "
            "saved{2}. The previous refresh token is already spent — run "
            "`makervox_publish auth {0} {1}` to re-authorize.".format(
                platform, account, ": " + detail if detail else ""
            )
        )
        self.platform = platform
        self.account = account


class ReauthorizationRequired(MakervoxPublishError):
    """A scope or grant is missing and no refresh can add it.

    Scopes are FIXED at authorization time. Refreshing a token never widens
    them, so a scope error means re-auth, not retry — the vendor's own error
    text does not say so, which is why this class exists.
    """

    def __init__(self, platform: str, account: str, scopes: Sequence[str] = ()) -> None:
        want = ", ".join(scopes)
        super().__init__(
            "{0}/{1} needs re-authorization{2}. Scopes are fixed when the token "
            "is granted; refreshing does NOT add them. Run "
            "`makervox_publish auth {0} {1}`.".format(
                platform, account, " for scopes: " + want if want else ""
            )
        )
        self.platform = platform
        self.account = account
        self.scopes = list(scopes)


class IdentityClash(MakervoxPublishError):
    """An OAuth callback returned an account that belongs to a different label.

    Platforms issue a token for whichever account the BROWSER was signed into,
    not the one named on the command line. Saving it anyway files A's
    credentials under B and every later post lands on the wrong account.
    """

    def __init__(self, requested: str, existing: str, platform_account_id: str) -> None:
        super().__init__(
            "refusing to save credentials: the authorization came back for "
            "platform account {0}, which is already stored under the local "
            "account {1!r}, not {2!r}. Sign out of the other account in your "
            "browser and run the auth command again.".format(
                platform_account_id, existing, requested
            )
        )
        self.requested = requested
        self.existing = existing
        self.platform_account_id = platform_account_id


class LockUnavailable(MakervoxPublishError):
    """An advisory lock could not be acquired and policy says fail."""
