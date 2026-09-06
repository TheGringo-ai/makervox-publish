"""Frozen dataclasses, one per config block, plus the top-level :class:`Config`.

These dataclasses are the ONLY place defaults live. There is no shipped
defaults file to drift out of sync with them, and every ``from_cursor``
rejects unknown keys so a typo cannot silently disable a safety setting.

Two conventions carried through the whole schema:

* Anything only the app owner can supply — an OAuth redirect URI, a staging
  bucket, a cloud project id — has NO DEFAULT and raises ConfigError when it is
  needed. Defaulting those to somebody else's domain or project is both an
  identity leak and a silent-misconfiguration trap.
* Every allow-list and every piece of marketing copy defaults to empty. The
  package ships no CTA text and no account names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from makervox_publish.config.loader import (
    Cursor,
    deep_merge,
    env_overlay,
    expand_path,
    find_config_file,
    parse_file,
)
from makervox_publish.config.plugins import instantiate
from makervox_publish.errors import ConfigError
# The canonical connector list lives with the pure text transform that uses it,
# so the config default and the code default cannot drift apart. makervox_publish.text
# imports nothing from makervox_publish.config, so this direction stays acyclic.
from makervox_publish.text.shape import DEFAULT_TRAILING_CONNECTORS
from makervox_publish.version import CONFIG_SCHEMA_VERSION, LEDGER_SCHEMA_VERSION, user_agent

__all__ = [
    "PluginSpec",
    "LocksConfig",
    "StateConfig",
    "CredentialsConfig",
    "LoggingConfig",
    "CoverConfig",
    "ShortCutConfig",
    "TranscodeConfig",
    "MediaConfig",
    "HttpTimeouts",
    "RetryConfig",
    "HttpConfig",
    "FingerprintConfig",
    "IdentityConfig",
    "LedgerConfig",
    "StripConfig",
    "LinksConfig",
    "AccountConfig",
    "TikTokConfig",
    "FacebookConfig",
    "InstagramConfig",
    "XConfig",
    "PlatformsConfig",
    "AuthListenerConfig",
    "CliConfig",
    "Config",
]

_DEFAULT_STATE_DIR = os.path.join("~", ".local", "state", "makervox_publish")


def _state_path(state_dir: str, *parts: str) -> str:
    return expand_path(os.path.join(state_dir, *parts))


# --------------------------------------------------------------------------- #
# plugins
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PluginSpec:
    """``impl`` + ``options``: how a callable is injected from the config file."""

    impl: str
    options: Dict[str, Any] = field(default_factory=dict)
    key: str = ""

    @classmethod
    def from_cursor(cls, cur: Cursor, *, default_impl: Optional[str] = None) -> "PluginSpec":
        cur.reject_unknown(("impl", "options"))
        impl = cur.str("impl", default_impl)
        if not impl:
            raise ConfigError("missing 'impl'", key=cur.path, source=cur.source)
        return cls(impl=impl, options=cur.mapping("options"), key=cur.path)

    def build(self, *, group: Optional[str] = None, **extra: Any) -> Any:
        """Import and construct. ``extra`` is merged over ``options``."""
        options = dict(self.options)
        options.update(extra)
        return instantiate(self.impl, options, key=self.key or None, group=group)


# --------------------------------------------------------------------------- #
# state + locking
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LocksConfig:
    backend: str = "auto"            # auto | fcntl | msvcrt | none
    dir: str = ""                    # defaults under state.dir
    acquire_timeout_s: float = 120.0
    on_unavailable: str = "proceed"  # proceed | fail

    @classmethod
    def from_cursor(cls, cur: Cursor, state_dir: str) -> "LocksConfig":
        cur.reject_unknown(("backend", "dir", "acquire_timeout_s", "on_unavailable"))
        return cls(
            backend=cur.choice("backend", ("auto", "fcntl", "msvcrt", "none"), "auto"),
            dir=cur.path_str("dir") or _state_path(state_dir, "locks"),
            acquire_timeout_s=cur.float("acquire_timeout_s", 120.0, minimum=0.0),
            on_unavailable=cur.choice("on_unavailable", ("proceed", "fail"), "proceed"),
        )


@dataclass(frozen=True)
class StateConfig:
    """Every file this package writes lives under one root.

    Nothing is created in ``$HOME`` implicitly: directories are created on FIRST
    WRITE only, never at import time.
    """

    dir: str = expand_path(_DEFAULT_STATE_DIR)
    dir_mode: int = 0o700
    file_mode: int = 0o600
    enforce_file_mode: bool = True
    locks: LocksConfig = field(default_factory=LocksConfig)

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "StateConfig":
        cur.reject_unknown(("dir", "dir_mode", "file_mode", "enforce_file_mode", "locks"))
        state_dir = cur.path_str("dir") or expand_path(_DEFAULT_STATE_DIR)
        return cls(
            dir=state_dir,
            dir_mode=cur.int("dir_mode", 0o700),
            file_mode=cur.int("file_mode", 0o600),
            enforce_file_mode=cur.bool("enforce_file_mode", True),
            locks=LocksConfig.from_cursor(cur.child("locks"), state_dir),
        )

    def path(self, *parts: str) -> str:
        return _state_path(self.dir, *parts)


# --------------------------------------------------------------------------- #
# credentials + logging
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CredentialsConfig:
    providers: Tuple[PluginSpec, ...] = ()
    cache_ttl_s: float = 300.0
    redact_in_logs: bool = True

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "CredentialsConfig":
        cur.reject_unknown(("providers", "cache_ttl_s", "redact_in_logs"))
        return cls(
            providers=tuple(PluginSpec.from_cursor(c) for c in cur.table_list("providers")),
            cache_ttl_s=cur.float("cache_ttl_s", 300.0, minimum=0.0),
            redact_in_logs=cur.bool("redact_in_logs", True),
        )


@dataclass(frozen=True)
class LoggingConfig:
    logger_name: str = "makervox_publish"
    level: str = "INFO"
    swallowed_error_level: str = "WARNING"
    provider_error_body_chars: int = 400
    configure_root: bool = False

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "LoggingConfig":
        cur.reject_unknown(("logger_name", "level", "swallowed_error_level",
                            "provider_error_body_chars", "configure_root"))
        levels = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET")
        return cls(
            logger_name=cur.str("logger_name", "makervox_publish"),
            level=cur.choice("level", levels, "INFO"),
            swallowed_error_level=cur.choice("swallowed_error_level", levels, "WARNING"),
            provider_error_body_chars=cur.int("provider_error_body_chars", 400, minimum=0),
            configure_root=cur.bool("configure_root", False),
        )


# --------------------------------------------------------------------------- #
# media
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CoverConfig:
    """Cover-frame picker settings.

    Lives under ``media``, not under ``instagram``, because BOTH the Instagram
    and the Facebook publisher need it — that shared need is what produced the
    original circular import between the two modules.
    """

    enabled: bool = True
    impl: str = "makervox_publish.media.cover:BrightnessScanCoverPicker"
    scan_until_s: float = 6.0
    step_s: float = 0.25
    brightness_floor: float = 10.0
    min_offset_s: float = 0.75
    fallback_floor: float = 5.0
    take_first_qualifying: bool = True

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "CoverConfig":
        cur.reject_unknown(("enabled", "impl", "options"))
        opts = cur.child("options")
        opts.reject_unknown(("scan_until_s", "step_s", "brightness_floor",
                             "min_offset_s", "fallback_floor", "take_first_qualifying"))
        step = opts.float("step_s", 0.25, minimum=0.001)
        return cls(
            enabled=cur.bool("enabled", True),
            impl=cur.str("impl", "makervox_publish.media.cover:BrightnessScanCoverPicker"),
            scan_until_s=opts.float("scan_until_s", 6.0, minimum=0.0),
            step_s=step,
            brightness_floor=opts.float("brightness_floor", 10.0, minimum=0.0),
            min_offset_s=opts.float("min_offset_s", 0.75, minimum=0.0),
            fallback_floor=opts.float("fallback_floor", 5.0, minimum=0.0),
            take_first_qualifying=opts.bool("take_first_qualifying", True),
        )


@dataclass(frozen=True)
class ShortCutConfig:
    enabled: bool = True
    seconds: float = 15.0
    fade_s: float = 0.6
    skip_if_duration_under_s: float = 17.0
    encoder_args: Tuple[str, ...] = (
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-movflags", "+faststart",
    )
    timeout_s: float = 300.0

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "ShortCutConfig":
        cur.reject_unknown(("enabled", "seconds", "fade_s", "skip_if_duration_under_s",
                            "encoder_args", "timeout_s"))
        defaults = cls()
        return cls(
            enabled=cur.bool("enabled", True),
            seconds=cur.float("seconds", 15.0, minimum=0.1),
            fade_s=cur.float("fade_s", 0.6, minimum=0.0),
            skip_if_duration_under_s=cur.float("skip_if_duration_under_s", 17.0, minimum=0.0),
            encoder_args=cur.str_list("encoder_args", defaults.encoder_args),
            timeout_s=cur.float("timeout_s", 300.0, minimum=1.0),
        )


@dataclass(frozen=True)
class TranscodeConfig:
    enabled: bool = True
    encoder_args: Tuple[str, ...] = (
        "-c:v", "libx264", "-crf", "24", "-maxrate", "9M", "-bufsize", "18M",
        "-preset", "veryfast", "-c:a", "aac", "-movflags", "+faststart",
    )
    timeout_s: float = 900.0
    #: raise | use_original. Handing back the oversized original reproduces the
    #: exact upload rejection the transcode exists to prevent, so the default
    #: is to fail loudly.
    on_failure: str = "raise"

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "TranscodeConfig":
        cur.reject_unknown(("enabled", "encoder_args", "timeout_s", "on_failure"))
        defaults = cls()
        return cls(
            enabled=cur.bool("enabled", True),
            encoder_args=cur.str_list("encoder_args", defaults.encoder_args),
            timeout_s=cur.float("timeout_s", 900.0, minimum=1.0),
            on_failure=cur.choice("on_failure", ("raise", "use_original"), "raise"),
        )


@dataclass(frozen=True)
class MediaConfig:
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    require_ffmpeg: bool = False
    cover: CoverConfig = field(default_factory=CoverConfig)
    short_cut: ShortCutConfig = field(default_factory=ShortCutConfig)
    transcode: TranscodeConfig = field(default_factory=TranscodeConfig)

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "MediaConfig":
        cur.reject_unknown(("ffmpeg_path", "ffprobe_path", "require_ffmpeg",
                            "cover", "short_cut", "transcode"))
        return cls(
            ffmpeg_path=cur.str("ffmpeg_path", "ffmpeg"),
            ffprobe_path=cur.str("ffprobe_path", "ffprobe"),
            require_ffmpeg=cur.bool("require_ffmpeg", False),
            cover=CoverConfig.from_cursor(cur.child("cover")),
            short_cut=ShortCutConfig.from_cursor(cur.child("short_cut")),
            transcode=TranscodeConfig.from_cursor(cur.child("transcode")),
        )


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HttpTimeouts:
    default: float = 30.0
    auth: float = 60.0
    small_write: float = 60.0
    media_create: float = 120.0
    poll: float = 30.0
    upload_chunk: float = 600.0
    insights: float = 60.0

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "HttpTimeouts":
        names = ("default", "auth", "small_write", "media_create", "poll",
                 "upload_chunk", "insights")
        cur.reject_unknown(names)
        defaults = cls()
        values = {n: cur.float(n, getattr(defaults, n), minimum=0.1) for n in names}
        return cls(**values)


@dataclass(frozen=True)
class RetryConfig:
    """ONLY transport-level failures retry.

    API REJECTIONS ARE NEVER RETRIED: retrying a spam or rate rejection makes it
    worse, and on X it costs money per attempt.
    """

    attempts: int = 3
    backoff_s: float = 3.0
    retry_on: Tuple[str, ...] = ("transport",)

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "RetryConfig":
        cur.reject_unknown(("attempts", "backoff_s", "retry_on"))
        retry_on = cur.str_list("retry_on", ("transport",))
        for item in retry_on:
            if item not in ("transport", "5xx", "429"):
                raise ConfigError(
                    "unknown retry class {0!r}; use transport, 5xx or 429".format(item),
                    key=cur.path + ".retry_on", source=cur.source,
                )
        return cls(
            attempts=cur.int("attempts", 3, minimum=1),
            backoff_s=cur.float("backoff_s", 3.0, minimum=0.0),
            retry_on=retry_on,
        )


@dataclass(frozen=True)
class HttpConfig:
    user_agent: str = field(default_factory=user_agent)
    timeouts_s: HttpTimeouts = field(default_factory=HttpTimeouts)
    retry: RetryConfig = field(default_factory=RetryConfig)

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "HttpConfig":
        cur.reject_unknown(("user_agent", "timeouts_s", "retry"))
        return cls(
            user_agent=cur.str("user_agent", user_agent()),
            timeouts_s=HttpTimeouts.from_cursor(cur.child("timeouts_s")),
            retry=RetryConfig.from_cursor(cur.child("retry")),
        )


# --------------------------------------------------------------------------- #
# identity + ledger
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FingerprintConfig:
    algorithm: str = "sha256"
    #: FROZEN. Changing this orphans every existing ledger row.
    hex_chars: int = 16
    #: Hash the SOURCE media, never the derived/temp file that goes over the
    #: wire: one code path may upload a 15s cut while a recovery path uploads
    #: the full clip, and hashing the wire bytes would never match across them.
    hash_source_media: bool = True

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "FingerprintConfig":
        cur.reject_unknown(("algorithm", "hex_chars", "hash_source_media"))
        return cls(
            algorithm=cur.choice("algorithm", ("sha256", "sha1", "blake2b"), "sha256"),
            hex_chars=cur.int("hex_chars", 16, minimum=8, maximum=64),
            hash_source_media=cur.bool("hash_source_media", True),
        )


@dataclass(frozen=True)
class IdentityConfig:
    """A FILENAME IS NOT AN IDENTITY.

    A scheduler that renders the same content type twice in one day overwrites
    the first file, so two genuinely different videos share one name. Dedupe on
    ``(account, date, key, slot, content hash)`` instead.
    """

    impl: str = "makervox_publish.identity.post_id:FilenamePostIdentity"
    options: Dict[str, Any] = field(default_factory=dict)
    fingerprint: FingerprintConfig = field(default_factory=FingerprintConfig)
    #: warn_and_publish | refuse | publish. Fails OPEN by default (a rare
    #: duplicate beats silently suppressing a real post) but SAYS SO.
    on_unresolved: str = "warn_and_publish"

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "IdentityConfig":
        cur.reject_unknown(("impl", "options", "fingerprint", "on_unresolved"))
        default_options = {
            "date_pattern": r"(\d{4}-\d{2}-\d{2})",
            "slot_pattern": r"__s(\d+)(?=\.[^.]*$|$)",
            "default_slot": 1,
        }
        options = cur.mapping("options") or default_options
        return cls(
            impl=cur.str("impl", "makervox_publish.identity.post_id:FilenamePostIdentity"),
            options=options,
            fingerprint=FingerprintConfig.from_cursor(cur.child("fingerprint")),
            on_unresolved=cur.choice(
                "on_unresolved", ("warn_and_publish", "refuse", "publish"),
                "warn_and_publish",
            ),
        )


@dataclass(frozen=True)
class LedgerConfig:
    """The delivery ledger is PUBLIC API, not an internal file.

    External recovery and reporting tooling reads these rows, so the schema is
    versioned and frozen and new fields are additive only. v1 rows (no ``sha``,
    no ``slot``) are tolerated and treated as "identity unknown".
    """

    impl: str = "makervox_publish.state.ledger:JsonlLedger"
    options: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = LEDGER_SCHEMA_VERSION

    @classmethod
    def from_cursor(cls, cur: Cursor, state: StateConfig) -> "LedgerConfig":
        cur.reject_unknown(("impl", "options", "schema_version"))
        options = cur.mapping("options")
        options.setdefault("path", state.path("delivered.jsonl"))
        if isinstance(options.get("path"), str):
            options["path"] = expand_path(options["path"])
        return cls(
            impl=cur.str("impl", "makervox_publish.state.ledger:JsonlLedger"),
            options=options,
            schema_version=cur.int("schema_version", LEDGER_SCHEMA_VERSION, minimum=1),
        )


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StripConfig:
    """Removing link-bearing lines from a description.

    ``tld_allowlist`` is a trap: an unlisted TLD passes through unstripped into
    a description that was supposed to be link-free. ``any_dot_tld`` is the
    default for that reason.
    """

    mode: str = "any_dot_tld"          # urls_only | any_dot_tld | tld_allowlist
    tld_allowlist: Tuple[str, ...] = ()
    keep_hashtags: bool = True
    collapse_blank_lines: bool = True

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "StripConfig":
        cur.reject_unknown(("mode", "tld_allowlist", "keep_hashtags", "collapse_blank_lines"))
        mode = cur.choice("mode", ("urls_only", "any_dot_tld", "tld_allowlist"), "any_dot_tld")
        allowlist = cur.str_list("tld_allowlist")
        if mode == "tld_allowlist" and not allowlist:
            raise ConfigError(
                "mode is 'tld_allowlist' but tld_allowlist is empty, so nothing "
                "would ever be stripped",
                key=cur.path or "links.strip", source=cur.source,
            )
        return cls(
            mode=mode,
            tld_allowlist=allowlist,
            keep_hashtags=cur.bool("keep_hashtags", True),
            collapse_blank_lines=cur.bool("collapse_blank_lines", True),
        )


@dataclass(frozen=True)
class LinksConfig:
    #: Default is a NO-OP tagger. A tagger hardcoded to one domain silently
    #: ships every other brand's links untagged, with no error at all.
    tagger: PluginSpec = field(
        default_factory=lambda: PluginSpec("makervox_publish.text.links:NoopTagger", {})
    )
    strip: StripConfig = field(default_factory=StripConfig)

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "LinksConfig":
        cur.reject_unknown(("tagger", "strip"))
        tagger_cur = cur.child("tagger")
        tagger = (
            PluginSpec.from_cursor(tagger_cur)
            if tagger_cur.data
            else PluginSpec("makervox_publish.text.links:NoopTagger", {})
        )
        return cls(tagger=tagger, strip=StripConfig.from_cursor(cur.child("strip")))


# --------------------------------------------------------------------------- #
# accounts
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AccountConfig:
    """One brand/persona you publish as.

    The key is YOUR label; nothing is special-cased on it. No account name is
    baked into this package.
    """

    name: str
    display_name: str = ""
    first_comment: str = ""     # the package ships no marketing copy
    link_url: Optional[str] = None
    platforms: Tuple[str, ...] = ()
    facebook_page_id: Optional[str] = None
    instagram_user_id: Optional[str] = None
    tiktok_scopes: Optional[str] = None
    tiktok_auto_publish: Optional[bool] = None

    @classmethod
    def from_cursor(cls, name: str, cur: Cursor) -> "AccountConfig":
        cur.reject_unknown(("display_name", "first_comment", "link_url", "platforms",
                            "facebook", "instagram", "tiktok"))
        fb = cur.child("facebook")
        fb.reject_unknown(("page_id",))
        ig = cur.child("instagram")
        ig.reject_unknown(("ig_user_id",))
        tt = cur.child("tiktok")
        tt.reject_unknown(("scopes", "auto_publish"))
        return cls(
            name=name,
            display_name=cur.str("display_name", name),
            first_comment=cur.str("first_comment", "") or "",
            link_url=cur.str("link_url"),
            platforms=cur.str_list("platforms"),
            facebook_page_id=fb.str("page_id"),
            instagram_user_id=ig.str("ig_user_id"),
            tiktok_scopes=tt.str("scopes"),
            tiktok_auto_publish=tt.bool("auto_publish"),
        )


# --------------------------------------------------------------------------- #
# platforms
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TikTokTokensConfig:
    store: str = "tiktok_tokens"
    cache_ttl_s: float = 60.0
    expiry_skew_s: float = 60.0
    refresh_lock: str = "tiktok-refresh"
    #: TikTok ROTATES THE REFRESH TOKEN ON USE: a successful refresh SPENDS the
    #: old one, so two concurrent refreshes race destructively and the loser
    #: needs a manual re-auth. Three defences, all on by default.
    lock_refresh: bool = True
    reread_inside_lock: bool = True
    adopt_remote_on_reject: bool = True

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "TikTokTokensConfig":
        cur.reject_unknown(("store", "cache_ttl_s", "expiry_skew_s", "refresh_lock",
                            "lock_refresh", "reread_inside_lock", "adopt_remote_on_reject"))
        return cls(
            store=cur.str("store", "tiktok_tokens"),
            cache_ttl_s=cur.float("cache_ttl_s", 60.0, minimum=0.0),
            expiry_skew_s=cur.float("expiry_skew_s", 60.0, minimum=0.0),
            refresh_lock=cur.str("refresh_lock", "tiktok-refresh"),
            lock_refresh=cur.bool("lock_refresh", True),
            reread_inside_lock=cur.bool("reread_inside_lock", True),
            adopt_remote_on_reject=cur.bool("adopt_remote_on_reject", True),
        )


@dataclass(frozen=True)
class TikTokUploadConfig:
    #: 20 MiB. TikTok's documented per-chunk ceiling is 64 MiB; 20 MiB is an
    #: EMPIRICAL reliability setting. Both numbers are real — do not "correct"
    #: one from the other.
    max_chunk_bytes: int = 20971520
    #: 60 MiB. Larger reels 403 with "invalid request id"; transcode first.
    max_video_bytes: int = 62914560
    accepted_statuses: Tuple[int, ...] = (200, 201, 206)   # 206 IS success
    caption_max_chars: int = 2200

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "TikTokUploadConfig":
        cur.reject_unknown(("max_chunk_bytes", "max_video_bytes", "accepted_statuses",
                            "caption_max_chars"))
        return cls(
            max_chunk_bytes=cur.int("max_chunk_bytes", 20971520, minimum=5 * 1024 * 1024),
            max_video_bytes=cur.int("max_video_bytes", 62914560, minimum=1024 * 1024),
            accepted_statuses=cur.int_list("accepted_statuses", (200, 201, 206)),
            caption_max_chars=cur.int("caption_max_chars", 2200, minimum=1),
        )


@dataclass(frozen=True)
class TikTokConfirmConfig:
    attempts: int = 6
    delay_s: float = 4.0
    #: Still PROCESSING after the budget is treated as accepted rather than
    #: reported as a failure — the upload is in TikTok's hands by then.
    processing_is_accepted: bool = True

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "TikTokConfirmConfig":
        cur.reject_unknown(("attempts", "delay_s", "processing_is_accepted"))
        return cls(
            attempts=cur.int("attempts", 6, minimum=1),
            delay_s=cur.float("delay_s", 4.0, minimum=0.0),
            processing_is_accepted=cur.bool("processing_is_accepted", True),
        )


@dataclass(frozen=True)
class TikTokPublishConfig:
    mode: str = "inbox"                # inbox | direct
    #: 0 = NEVER cache. A creator can flip to private at any moment, and the
    #: API forbids a pre-selected privacy level, so the probe must be fresh.
    creator_info_cache_s: float = 0.0
    refuse_branded_private: bool = True
    #: DEFAULT IS EMPTY. The original shipped two real account names here.
    auto_publish_disabled_accounts: Tuple[str, ...] = ()
    confirm: TikTokConfirmConfig = field(default_factory=TikTokConfirmConfig)

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "TikTokPublishConfig":
        cur.reject_unknown(("mode", "creator_info_cache_s", "refuse_branded_private",
                            "auto_publish_disabled_accounts", "confirm"))
        return cls(
            mode=cur.choice("mode", ("inbox", "direct"), "inbox"),
            creator_info_cache_s=cur.float("creator_info_cache_s", 0.0, minimum=0.0),
            refuse_branded_private=cur.bool("refuse_branded_private", True),
            auto_publish_disabled_accounts=cur.str_list("auto_publish_disabled_accounts"),
            confirm=TikTokConfirmConfig.from_cursor(cur.child("confirm")),
        )


@dataclass(frozen=True)
class TikTokMetricsConfig:
    list_page_size: int = 20           # the API caps video/list max_count at 20
    max_videos: int = 400
    follower_log_enabled: bool = False
    follower_log_path: Optional[str] = None
    #: One row per UTC day, never two: a job that runs twice would otherwise
    #: turn a flat day into a fake datapoint when the series is plotted.
    follower_log_one_row_per_day: bool = True

    @classmethod
    def from_cursor(cls, cur: Cursor, state: StateConfig) -> "TikTokMetricsConfig":
        cur.reject_unknown(("list_page_size", "max_videos", "follower_log"))
        flog = cur.child("follower_log")
        flog.reject_unknown(("enabled", "path", "one_row_per_day"))
        return cls(
            list_page_size=cur.int("list_page_size", 20, minimum=1, maximum=20),
            max_videos=cur.int("max_videos", 400, minimum=1),
            follower_log_enabled=flog.bool("enabled", False),
            follower_log_path=flog.path_str("path") or state.path("tiktok_followers.csv"),
            follower_log_one_row_per_day=flog.bool("one_row_per_day", True),
        )


@dataclass(frozen=True)
class TikTokConfig:
    enabled: bool = False
    client_key_credential: str = "TIKTOK_CLIENT_KEY"
    client_secret_credential: str = "TIKTOK_CLIENT_SECRET"
    #: REQUIRED when enabled. No default: it must match a redirect URI
    #: registered on YOUR TikTok app, and defaulting it to somebody else's
    #: domain is both an identity leak and a silent-misconfiguration trap.
    redirect_uri: Optional[str] = None
    #: SCOPES ARE FIXED AT AUTHORIZATION. Refreshing a token does NOT add them;
    #: changing this line does nothing until each account RE-AUTHORIZES.
    scopes: str = "user.info.basic,video.upload"
    api_base: str = "https://open.tiktokapis.com/v2"
    auth_base: str = "https://www.tiktok.com/v2/auth/authorize/"
    tokens: TikTokTokensConfig = field(default_factory=TikTokTokensConfig)
    upload: TikTokUploadConfig = field(default_factory=TikTokUploadConfig)
    publish: TikTokPublishConfig = field(default_factory=TikTokPublishConfig)
    metrics: TikTokMetricsConfig = field(default_factory=TikTokMetricsConfig)

    @classmethod
    def from_cursor(cls, cur: Cursor, state: StateConfig) -> "TikTokConfig":
        cur.reject_unknown(("enabled", "client_key_credential", "client_secret_credential",
                            "redirect_uri", "scopes", "api_base", "auth_base",
                            "tokens", "upload", "publish", "metrics"))
        enabled = cur.bool("enabled", False)
        redirect_uri = cur.str("redirect_uri")
        if enabled and not redirect_uri:
            raise ConfigError(
                "redirect_uri has no default: it must match a redirect URI "
                "registered on YOUR OWN TikTok app. A loopback URL such as "
                "http://127.0.0.1:8722/tiktok/callback works with "
                "`makervox_publish auth tiktok <account>`.",
                key=(cur.path + ".redirect_uri") if cur.path else "redirect_uri",
                source=cur.source,
            )
        return cls(
            enabled=enabled,
            client_key_credential=cur.str("client_key_credential", "TIKTOK_CLIENT_KEY"),
            client_secret_credential=cur.str("client_secret_credential",
                                             "TIKTOK_CLIENT_SECRET"),
            redirect_uri=redirect_uri,
            scopes=cur.str("scopes", "user.info.basic,video.upload"),
            api_base=cur.str("api_base", "https://open.tiktokapis.com/v2"),
            auth_base=cur.str("auth_base", "https://www.tiktok.com/v2/auth/authorize/"),
            tokens=TikTokTokensConfig.from_cursor(cur.child("tokens")),
            upload=TikTokUploadConfig.from_cursor(cur.child("upload")),
            publish=TikTokPublishConfig.from_cursor(cur.child("publish")),
            metrics=TikTokMetricsConfig.from_cursor(cur.child("metrics"), state),
        )

    def scopes_for(self, account: AccountConfig) -> str:
        return account.tiktok_scopes or self.scopes

    def auto_publish_for(self, account: AccountConfig) -> bool:
        if account.name in self.publish.auto_publish_disabled_accounts:
            return False
        if account.tiktok_auto_publish is not None:
            return account.tiktok_auto_publish
        return self.publish.mode == "direct"


@dataclass(frozen=True)
class FacebookFirstCommentConfig:
    enabled: bool = True
    #: ONE comment carries the readable body AND the single tagged CTA link, so
    #: there is no near-duplicate text post sitting next to the reel.
    include_body: bool = True
    utm_medium: str = "reel"
    #: The story object EXISTS before it accepts comments: the target id
    #: resolves almost immediately but POSTing to it right then returns
    #: "(#100) Unsupported post request". Resolving the id is NOT enough — the
    #: POST itself must be retried.
    target_resolve_attempts: int = 5
    target_resolve_backoff_s: float = 3.0
    post_attempts: int = 5
    post_backoff_s: float = 5.0       # 5+10+15+20 = 50s, covers the observed lag

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "FacebookFirstCommentConfig":
        cur.reject_unknown(("enabled", "include_body", "utm_medium",
                            "target_resolve_attempts", "target_resolve_backoff_s",
                            "post_attempts", "post_backoff_s"))
        return cls(
            enabled=cur.bool("enabled", True),
            include_body=cur.bool("include_body", True),
            utm_medium=cur.str("utm_medium", "reel"),
            target_resolve_attempts=cur.int("target_resolve_attempts", 5, minimum=1),
            target_resolve_backoff_s=cur.float("target_resolve_backoff_s", 3.0, minimum=0.0),
            post_attempts=cur.int("post_attempts", 5, minimum=1),
            post_backoff_s=cur.float("post_backoff_s", 5.0, minimum=0.0),
        )


@dataclass(frozen=True)
class FacebookReelsConfig:
    #: The 3-phase resumable upload mints ONE video_id per reel and every retry
    #: re-uses it. Retrying the OUTER function re-runs `start`, mints a NEW
    #: video_id, and a network drop after the platform already committed
    #: produces a SECOND reel.
    upload_retries: int = 3
    upload_backoff_s: float = 3.0
    tolerate_already_published: bool = True
    #: Reels have no impressions/reach metric in this API version; plays are
    #: the proxy, and they come from insights, not the `views` field.
    insight_metrics: Tuple[str, ...] = ("blue_reels_play_count", "fb_reels_total_plays")
    #: This platform accepts a new preferred thumbnail on an EXISTING video
    #: (unlike Instagram, where the cover is settable only at publish time), so
    #: this runs after publishing and never blocks it.
    set_cover_after_publish: bool = True
    strip_links_from_description: bool = True
    first_comment: FacebookFirstCommentConfig = field(
        default_factory=FacebookFirstCommentConfig
    )
    #: Post the short cut instead of the master. The LEDGER still records the
    #: SOURCE file, not the temp cut.
    use_short_cut: bool = True

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "FacebookReelsConfig":
        cur.reject_unknown(("upload_retries", "upload_backoff_s",
                            "tolerate_already_published", "insight_metrics",
                            "set_cover_after_publish", "strip_links_from_description",
                            "first_comment", "use_short_cut"))
        defaults = cls()
        return cls(
            upload_retries=cur.int("upload_retries", 3, minimum=1),
            upload_backoff_s=cur.float("upload_backoff_s", 3.0, minimum=0.0),
            tolerate_already_published=cur.bool("tolerate_already_published", True),
            insight_metrics=cur.str_list("insight_metrics", defaults.insight_metrics),
            set_cover_after_publish=cur.bool("set_cover_after_publish", True),
            strip_links_from_description=cur.bool("strip_links_from_description", True),
            first_comment=FacebookFirstCommentConfig.from_cursor(cur.child("first_comment")),
            use_short_cut=cur.bool("use_short_cut", True),
        )


@dataclass(frozen=True)
class DedupeConfig:
    #: The per-account publish lock only SERIALIZES concurrent callers; it does
    #: NOT deduplicate. Caller #2 waits for #1 and then publishes the same reel
    #: again, so the delivery re-check must happen INSIDE the lock — a check
    #: made before acquiring it is a TOCTOU whose snapshot goes stale while
    #: blocked.
    check_inside_lock: bool = True
    #: A suppressed duplicate returns ok=True: the post IS on the Page, which is
    #: what both callers were asking for.
    duplicate_result_is_ok: bool = True

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "DedupeConfig":
        cur.reject_unknown(("check_inside_lock", "duplicate_result_is_ok"))
        check_inside_lock = cur.bool("check_inside_lock", True)
        if not check_inside_lock:
            raise ConfigError(
                "check_inside_lock = false reintroduces the duplicate-publish "
                "TOCTOU this package exists to prevent; it is configurable only "
                "so it can be named, not so it can be turned off.",
                key=cur.path or "dedupe.check_inside_lock", source=cur.source,
            )
        return cls(
            check_inside_lock=True,
            duplicate_result_is_ok=cur.bool("duplicate_result_is_ok", True),
        )


@dataclass(frozen=True)
class FacebookConfig:
    enabled: bool = False
    app_id_credential: str = "META_APP_ID"
    app_secret_credential: str = "META_APP_SECRET"
    #: ONE key drives both hosts; they must stay in lockstep.
    api_version: str = "v21.0"
    graph_base: str = "https://graph.facebook.com"
    rupload_base: str = "https://rupload.facebook.com/video-upload"
    tokens_store: str = "meta_tokens"
    reels: FacebookReelsConfig = field(default_factory=FacebookReelsConfig)
    dedupe: DedupeConfig = field(default_factory=DedupeConfig)

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "FacebookConfig":
        cur.reject_unknown(("enabled", "app_id_credential", "app_secret_credential",
                            "api_version", "graph_base", "rupload_base", "tokens",
                            "reels", "dedupe"))
        tokens = cur.child("tokens")
        tokens.reject_unknown(("store",))
        return cls(
            enabled=cur.bool("enabled", False),
            app_id_credential=cur.str("app_id_credential", "META_APP_ID"),
            app_secret_credential=cur.str("app_secret_credential", "META_APP_SECRET"),
            api_version=cur.str("api_version", "v21.0"),
            graph_base=cur.str("graph_base", "https://graph.facebook.com"),
            rupload_base=cur.str("rupload_base",
                                 "https://rupload.facebook.com/video-upload"),
            tokens_store=tokens.str("store", "meta_tokens"),
            reels=FacebookReelsConfig.from_cursor(cur.child("reels")),
            dedupe=DedupeConfig.from_cursor(cur.child("dedupe")),
        )


@dataclass(frozen=True)
class InstagramPublishConfig:
    poll_attempts: int = 30            # reels MUST reach FINISHED before publish
    poll_interval_s: float = 6.0       # 30 x 6s = 3 minutes
    media_type_from_extension: Tuple[str, ...] = (".mp4", ".mov")
    send_thumb_offset: bool = True     # milliseconds, from media.cover

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "InstagramPublishConfig":
        cur.reject_unknown(("poll_attempts", "poll_interval_s",
                            "media_type_from_extension", "send_thumb_offset"))
        defaults = cls()
        return cls(
            poll_attempts=cur.int("poll_attempts", 30, minimum=1),
            poll_interval_s=cur.float("poll_interval_s", 6.0, minimum=0.5),
            media_type_from_extension=cur.str_list(
                "media_type_from_extension", defaults.media_type_from_extension
            ),
            send_thumb_offset=cur.bool("send_thumb_offset", True),
        )


@dataclass(frozen=True)
class InstagramConfig:
    """Reels/photos via the linked Facebook Page.

    PRIMARY path: a Professional IG account linked to a Facebook Page is
    published via ``graph.facebook.com/{ig-user-id}`` using the PAGE token — no
    separate Instagram-login token to mint. That sidesteps the Instagram-login
    OAuth entirely.
    """

    enabled: bool = False
    auth_path: str = "facebook_page"   # facebook_page | instagram_login
    api_version: Optional[str] = None  # inherits facebook.api_version when unset
    graph_base: str = "https://graph.facebook.com"
    instagram_graph_base: str = "https://graph.instagram.com"
    tokens_store: str = "meta_tokens"  # SAME store as facebook: one file, one lock
    #: Long-lived Instagram-login tokens last 60d; refresh past 50d.
    tokens_refresh_after_s: float = 4320000.0
    #: Instagram PULLS media by URL — you cannot upload bytes. There is no
    #: default staging target because only you know where you can serve files.
    staging: Optional[PluginSpec] = None
    publish: InstagramPublishConfig = field(default_factory=InstagramPublishConfig)

    @classmethod
    def from_cursor(cls, cur: Cursor, facebook: FacebookConfig) -> "InstagramConfig":
        cur.reject_unknown(("enabled", "auth_path", "api_version", "graph_base",
                            "instagram_graph_base", "tokens", "staging", "publish"))
        tokens = cur.child("tokens")
        tokens.reject_unknown(("store", "refresh_after_s"))
        enabled = cur.bool("enabled", False)
        staging_cur = cur.child("staging")
        staging = PluginSpec.from_cursor(staging_cur) if staging_cur.data else None
        if enabled and staging is None:
            raise ConfigError(
                "Instagram can only PULL media by URL, so it needs a staging "
                "target and there is no default one. Set "
                "platforms.instagram.staging.impl (see "
                "makervox_publish.platforms.meta.staging).",
                key=(cur.path + ".staging") if cur.path else "staging",
                source=cur.source,
            )
        return cls(
            enabled=enabled,
            auth_path=cur.choice("auth_path", ("facebook_page", "instagram_login"),
                                 "facebook_page"),
            api_version=cur.str("api_version", facebook.api_version),
            graph_base=cur.str("graph_base", "https://graph.facebook.com"),
            instagram_graph_base=cur.str("instagram_graph_base",
                                         "https://graph.instagram.com"),
            tokens_store=tokens.str("store", "meta_tokens"),
            tokens_refresh_after_s=tokens.float("refresh_after_s", 4320000.0, minimum=0.0),
            staging=staging,
            publish=InstagramPublishConfig.from_cursor(cur.child("publish")),
        )


@dataclass(frozen=True)
class XGovernorConfig:
    """Suppression is ONE-WAY.

    An account that behaves like a marketing bot gets suppressed, and posting
    more cannot undo it. Every rule here trades reach per post for the account
    staying visible at all.
    """

    state: PluginSpec = field(
        default_factory=lambda: PluginSpec("makervox_publish.state.counters:JsonCounterStore", {})
    )
    #: ONE post per ACCOUNT per day. Not per brand: one set of credentials is
    #: one account, and a per-brand counter lets two brands each spend their own
    #: "1 per day" onto the same account.
    daily_cap: int = 1
    timezone: str = "UTC"
    recent_keep: int = 30
    #: ALLOW-LIST, never a deny-list. EMPTY MEANS DENY ALL — a shared publish
    #: path otherwise grants posting rights to any account passing through it.
    enabled_accounts: Tuple[str, ...] = ()
    #: Deliberate asymmetry: a listed slug is allowed, an unlisted slug is
    #: DENIED, and a missing/empty slug is ALLOWED — refusing on missing
    #: metadata silently stops posting, the hardest failure to notice.
    allowed_slugs: Tuple[str, ...] = ()

    @classmethod
    def from_cursor(cls, cur: Cursor, state_cfg: StateConfig) -> "XGovernorConfig":
        cur.reject_unknown(("state", "daily_cap", "timezone", "recent_keep",
                            "enabled_accounts", "allowed_slugs"))
        state_cur = cur.child("state")
        if state_cur.data:
            spec = PluginSpec.from_cursor(state_cur)
        else:
            spec = PluginSpec("makervox_publish.state.counters:JsonCounterStore", {})
        options = dict(spec.options)
        options.setdefault("path", state_cfg.path("x_governor.json"))
        if isinstance(options.get("path"), str):
            options["path"] = expand_path(options["path"])
        spec = PluginSpec(spec.impl, options, spec.key)
        return cls(
            state=spec,
            daily_cap=cur.int("daily_cap", 1, minimum=0),
            timezone=cur.str("timezone", "UTC"),
            recent_keep=cur.int("recent_keep", 30, minimum=0),
            enabled_accounts=cur.str_list("enabled_accounts"),
            allowed_slugs=cur.str_list("allowed_slugs"),
        )

    def account_allowed(self, account: str) -> bool:
        """Deny-by-default. An empty allow-list allows NOTHING."""
        return account in self.enabled_accounts

    def slug_allowed(self, slug: Optional[str]) -> bool:
        """Missing slug -> allowed. Listed -> allowed. Unlisted -> DENIED."""
        if not self.allowed_slugs:
            return True
        if not slug:
            return True
        return slug in self.allowed_slugs


@dataclass(frozen=True)
class XShapeConfig:
    max_chars: int = 280
    truncate_to: int = 277             # max_chars - 3, room for the ellipsis
    min_sentence_boundary: int = 180
    min_body_chars: int = 20           # refuse to post a stub
    max_hashtags: int = 2              # a hashtag wall is a spam signal here
    strip_urls: bool = True
    #: A SET, not a growing regex: the failure mode is a phrasing nobody
    #: anticipated, and adding a word is the whole fix. English-only.
    #:
    #: The default is the accumulated list, not a tidy subset: every word here
    #: was added because a real caption ended in it once the URL was removed.
    #: Trimming it to the obvious prepositions leaves "Full reading" hanging off
    #: the end of a post, which is the exact defect the sweep exists to prevent.
    trailing_connectors: Tuple[str, ...] = DEFAULT_TRAILING_CONNECTORS

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "XShapeConfig":
        cur.reject_unknown(("max_chars", "truncate_to", "min_sentence_boundary",
                            "min_body_chars", "max_hashtags", "strip_urls",
                            "trailing_connectors"))
        defaults = cls()
        max_chars = cur.int("max_chars", 280, minimum=1)
        truncate_to = cur.int("truncate_to", max_chars - 3, minimum=1)
        if truncate_to > max_chars:
            raise ConfigError(
                "truncate_to ({0}) must not exceed max_chars ({1})".format(
                    truncate_to, max_chars
                ),
                key=(cur.path + ".truncate_to") if cur.path else "truncate_to",
                source=cur.source,
            )
        return cls(
            max_chars=max_chars,
            truncate_to=truncate_to,
            min_sentence_boundary=cur.int("min_sentence_boundary", 180, minimum=0),
            min_body_chars=cur.int("min_body_chars", 20, minimum=0),
            max_hashtags=cur.int("max_hashtags", 2, minimum=0),
            strip_urls=cur.bool("strip_urls", True),
            trailing_connectors=cur.str_list("trailing_connectors",
                                             defaults.trailing_connectors),
        )


@dataclass(frozen=True)
class XLinkReplyConfig:
    #: A post CONTAINING A URL is billed at a much higher rate, so the link
    #: normally lives in the PROFILE BIO instead. Cost of that choice: bio
    #: traffic arrives as one bucket instead of per-post UTM attribution.
    enabled: bool = False
    template: str = "{cta} -> {url}"
    cta: str = ""
    utm_medium: str = "social"

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "XLinkReplyConfig":
        cur.reject_unknown(("enabled", "template", "cta", "utm_medium"))
        return cls(
            enabled=cur.bool("enabled", False),
            template=cur.str("template", "{cta} -> {url}"),
            cta=cur.str("cta", "") or "",
            utm_medium=cur.str("utm_medium", "social"),
        )


@dataclass(frozen=True)
class XPricingConfig:
    """Used ONLY by ``makervox_publish estimate`` / dry-run output.

    Point-in-time figures that WILL rot: a dry run that misstates the expensive
    branch is worse than no dry run, so ``verified_on`` ships alongside them.
    """

    price_per_post: float = 0.0
    price_per_post_with_url: float = 0.0
    currency: str = "USD"
    verified_on: Optional[str] = None

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "XPricingConfig":
        cur.reject_unknown(("price_per_post", "price_per_post_with_url", "currency",
                            "verified_on"))
        return cls(
            price_per_post=cur.float("price_per_post", 0.0, minimum=0.0),
            price_per_post_with_url=cur.float("price_per_post_with_url", 0.0, minimum=0.0),
            currency=cur.str("currency", "USD"),
            verified_on=cur.str("verified_on"),
        )


@dataclass(frozen=True)
class XMediaConfig:
    #: Stills only. Video needs chunked INIT/APPEND/FINALIZE plus status polling
    #: and is not implemented. An image is a BONUS, never a blocker.
    image_upload_enabled: bool = True
    image_failure_blocks_post: bool = False

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "XMediaConfig":
        cur.reject_unknown(("image_upload_enabled", "image_failure_blocks_post"))
        return cls(
            image_upload_enabled=cur.bool("image_upload_enabled", True),
            image_failure_blocks_post=cur.bool("image_failure_blocks_post", False),
        )


@dataclass(frozen=True)
class XConfig:
    enabled: bool = False
    #: OAuth 1.0a USER CONTEXT (not an OAuth 2.0 bearer). The app must be
    #: Read+Write and the access token REGENERATED AFTER setting permissions.
    api_key_credential: str = "X_API_KEY"
    api_secret_credential: str = "X_API_SECRET"
    access_token_credential: str = "X_ACCESS_TOKEN"
    access_secret_credential: str = "X_ACCESS_SECRET"
    api_base: str = "https://api.twitter.com/2"
    #: Media upload is still the LEGACY v1.1 host — a real mixed-generation
    #: quirk, not a typo.
    media_upload_url: str = "https://upload.twitter.com/1.1/media/upload.json"
    governor: XGovernorConfig = field(default_factory=XGovernorConfig)
    shape: XShapeConfig = field(default_factory=XShapeConfig)
    link_reply: XLinkReplyConfig = field(default_factory=XLinkReplyConfig)
    pricing: XPricingConfig = field(default_factory=XPricingConfig)
    media: XMediaConfig = field(default_factory=XMediaConfig)

    @classmethod
    def from_cursor(cls, cur: Cursor, state: StateConfig) -> "XConfig":
        cur.reject_unknown(("enabled", "api_key_credential", "api_secret_credential",
                            "access_token_credential", "access_secret_credential",
                            "api_base", "media_upload_url", "governor", "shape",
                            "link_reply", "pricing", "media"))
        return cls(
            enabled=cur.bool("enabled", False),
            api_key_credential=cur.str("api_key_credential", "X_API_KEY"),
            api_secret_credential=cur.str("api_secret_credential", "X_API_SECRET"),
            access_token_credential=cur.str("access_token_credential", "X_ACCESS_TOKEN"),
            access_secret_credential=cur.str("access_secret_credential", "X_ACCESS_SECRET"),
            api_base=cur.str("api_base", "https://api.twitter.com/2"),
            media_upload_url=cur.str("media_upload_url",
                                     "https://upload.twitter.com/1.1/media/upload.json"),
            governor=XGovernorConfig.from_cursor(cur.child("governor"), state),
            shape=XShapeConfig.from_cursor(cur.child("shape")),
            link_reply=XLinkReplyConfig.from_cursor(cur.child("link_reply")),
            pricing=XPricingConfig.from_cursor(cur.child("pricing")),
            media=XMediaConfig.from_cursor(cur.child("media")),
        )


@dataclass(frozen=True)
class PlatformsConfig:
    tiktok: TikTokConfig = field(default_factory=TikTokConfig)
    facebook: FacebookConfig = field(default_factory=FacebookConfig)
    instagram: InstagramConfig = field(default_factory=InstagramConfig)
    x: XConfig = field(default_factory=XConfig)

    @classmethod
    def from_cursor(cls, cur: Cursor, state: StateConfig) -> "PlatformsConfig":
        cur.reject_unknown(("tiktok", "facebook", "instagram", "x"))
        facebook = FacebookConfig.from_cursor(cur.child("facebook"))
        return cls(
            tiktok=TikTokConfig.from_cursor(cur.child("tiktok"), state),
            facebook=facebook,
            instagram=InstagramConfig.from_cursor(cur.child("instagram"), facebook),
            x=XConfig.from_cursor(cur.child("x"), state),
        )

    def names(self) -> Tuple[str, ...]:
        return ("tiktok", "facebook", "instagram", "x")

    def enabled_names(self) -> Tuple[str, ...]:
        return tuple(n for n in self.names() if getattr(getattr(self, n), "enabled", False))


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AuthListenerConfig:
    host: str = "127.0.0.1"
    port: int = 8722
    #: CROSS-ACCOUNT AUTH TRAP: platforms issue a token for whichever account
    #: the BROWSER was signed into, not the one named on the command line.
    #: Authorizing B while signed in as A silently saves A's credentials under
    #: B, and every later post lands on the wrong account.
    refuse_identity_clash: bool = True

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "AuthListenerConfig":
        cur.reject_unknown(("host", "port", "refuse_identity_clash"))
        clash = cur.bool("refuse_identity_clash", True)
        if not clash:
            raise ConfigError(
                "refuse_identity_clash = false lets an authorization for one "
                "account be filed under another; it is named here so it can be "
                "understood, not disabled.",
                key=cur.path or "cli.auth_listener.refuse_identity_clash",
                source=cur.source,
            )
        return cls(
            host=cur.str("host", "127.0.0.1"),
            port=cur.int("port", 8722, minimum=1, maximum=65535),
            refuse_identity_clash=True,
        )


@dataclass(frozen=True)
class CliConfig:
    #: Interpolated into every "run: <prog> auth <account>" hint, so a wrapper
    #: script's own name appears in its own error messages.
    program_name: str = "makervox_publish"
    auth_listener: AuthListenerConfig = field(default_factory=AuthListenerConfig)

    @classmethod
    def from_cursor(cls, cur: Cursor) -> "CliConfig":
        cur.reject_unknown(("program_name", "auth_listener"))
        return cls(
            program_name=cur.str("program_name", "makervox_publish"),
            auth_listener=AuthListenerConfig.from_cursor(cur.child("auth_listener")),
        )


# --------------------------------------------------------------------------- #
# the whole thing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Config:
    version: int = CONFIG_SCHEMA_VERSION
    state: StateConfig = field(default_factory=StateConfig)
    credentials: CredentialsConfig = field(default_factory=CredentialsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    ledger: LedgerConfig = field(default_factory=LedgerConfig)
    links: LinksConfig = field(default_factory=LinksConfig)
    accounts: Dict[str, AccountConfig] = field(default_factory=dict)
    platforms: PlatformsConfig = field(default_factory=PlatformsConfig)
    token_stores: Dict[str, PluginSpec] = field(default_factory=dict)
    cli: CliConfig = field(default_factory=CliConfig)
    source: Optional[str] = None

    _TOP_LEVEL = ("version", "state", "credentials", "logging", "media", "http",
                  "identity", "ledger", "links", "accounts", "platforms",
                  "token_stores", "cli")

    # -- construction ------------------------------------------------------- #
    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, source: Optional[str] = None) -> "Config":
        root = Cursor(data, "", source)
        root.reject_unknown(cls._TOP_LEVEL)

        version = root.int("version", CONFIG_SCHEMA_VERSION, minimum=1)
        if version != CONFIG_SCHEMA_VERSION:
            raise ConfigError(
                "this build understands config schema version {0}, the file "
                "declares {1}. Schema bumps are breaking; see "
                "docs/migrating-from-makervox.md.".format(CONFIG_SCHEMA_VERSION, version),
                key="version", source=source,
            )

        state = StateConfig.from_cursor(root.child("state"))
        accounts = {
            name: AccountConfig.from_cursor(name, cur)
            for name, cur in root.child("accounts").children()
        }
        token_stores = {}  # type: Dict[str, PluginSpec]
        for name, cur in root.child("token_stores").children():
            spec = PluginSpec.from_cursor(cur)
            options = dict(spec.options)
            options.setdefault("path", state.path("{0}.json".format(name)))
            if isinstance(options.get("path"), str):
                options["path"] = expand_path(options["path"])
            token_stores[name] = PluginSpec(spec.impl, options, spec.key)

        cfg = cls(
            version=version,
            state=state,
            credentials=CredentialsConfig.from_cursor(root.child("credentials")),
            logging=LoggingConfig.from_cursor(root.child("logging")),
            media=MediaConfig.from_cursor(root.child("media")),
            http=HttpConfig.from_cursor(root.child("http")),
            identity=IdentityConfig.from_cursor(root.child("identity")),
            ledger=LedgerConfig.from_cursor(root.child("ledger"), state),
            links=LinksConfig.from_cursor(root.child("links")),
            accounts=accounts,
            platforms=PlatformsConfig.from_cursor(root.child("platforms"), state),
            token_stores=token_stores,
            cli=CliConfig.from_cursor(root.child("cli")),
            source=source,
        )
        cfg.validate()
        return cfg

    @classmethod
    def load(
        cls,
        path: Optional[str] = None,
        *,
        overrides: Optional[Mapping[str, Any]] = None,
        use_env_overlay: bool = True,
        cwd: Optional[str] = None,
    ) -> "Config":
        """Find, parse and validate the config file.

        With no file anywhere, every default applies and nothing is created on
        disk — ``makervox_publish`` is usable with environment variables alone.

        Precedence, lowest to highest: file, ``MAKERVOX_PUBLISH_*`` environment overlay,
        explicit ``overrides`` from the caller.
        """
        source = expand_path(path) if path else find_config_file(cwd)
        data = parse_file(source) if source else {}  # type: Mapping[str, Any]
        if use_env_overlay:
            data = deep_merge(data, env_overlay())
        if overrides:
            data = deep_merge(data, overrides)
        return cls.from_mapping(data, source=source)

    @classmethod
    def defaults(cls) -> "Config":
        """Every default, no file, no environment. Useful in tests."""
        return cls.from_mapping({})

    # -- cross-block validation --------------------------------------------- #
    def validate(self) -> None:
        known_platforms = set(self.platforms.names())

        for name, account in self.accounts.items():
            for platform in account.platforms:
                if platform not in known_platforms:
                    raise ConfigError(
                        "unknown platform {0!r}; known platforms are {1}".format(
                            platform, sorted(known_platforms)
                        ),
                        key="accounts.{0}.platforms".format(name), source=self.source,
                    )
                if not getattr(getattr(self.platforms, platform), "enabled", False):
                    raise ConfigError(
                        "account lists {0!r} but platforms.{0}.enabled is false, so "
                        "nothing would ever be posted there".format(platform),
                        key="accounts.{0}.platforms".format(name), source=self.source,
                    )
            if "facebook" in account.platforms and not account.facebook_page_id:
                raise ConfigError(
                    "publishing to facebook needs the Page id; only you can "
                    "supply it.",
                    key="accounts.{0}.facebook.page_id".format(name), source=self.source,
                )

        for platform, store in (("facebook", self.platforms.facebook.tokens_store),
                                ("instagram", self.platforms.instagram.tokens_store),
                                ("tiktok", self.platforms.tiktok.tokens.store)):
            if not getattr(getattr(self.platforms, platform), "enabled", False):
                continue
            if store not in self.token_stores:
                raise ConfigError(
                    "token store {0!r} is not defined; add a [token_stores.{0}] "
                    "table (known stores: {1})".format(store, sorted(self.token_stores)),
                    key="platforms.{0}.tokens.store".format(platform), source=self.source,
                )

        for account in self.platforms.x.governor.enabled_accounts:
            if account not in self.accounts:
                raise ConfigError(
                    "unknown account {0!r}; known accounts are {1}".format(
                        account, sorted(self.accounts)
                    ),
                    key="platforms.x.governor.enabled_accounts", source=self.source,
                )

        for account in self.platforms.tiktok.publish.auto_publish_disabled_accounts:
            if account not in self.accounts:
                raise ConfigError(
                    "unknown account {0!r}; known accounts are {1}".format(
                        account, sorted(self.accounts)
                    ),
                    key="platforms.tiktok.publish.auto_publish_disabled_accounts",
                    source=self.source,
                )

    # -- lookups ------------------------------------------------------------ #
    def account(self, name: str) -> AccountConfig:
        try:
            return self.accounts[name]
        except KeyError:
            raise ConfigError(
                "unknown account {0!r}; known accounts are {1}".format(
                    name, sorted(self.accounts)
                ),
                key="accounts", source=self.source,
            )

    def token_store_spec(self, name: str) -> PluginSpec:
        try:
            return self.token_stores[name]
        except KeyError:
            raise ConfigError(
                "unknown token store {0!r}; known stores are {1}".format(
                    name, sorted(self.token_stores)
                ),
                key="token_stores", source=self.source,
            )

    def platform(self, name: str) -> Any:
        if name not in self.platforms.names():
            raise ConfigError(
                "unknown platform {0!r}; known platforms are {1}".format(
                    name, list(self.platforms.names())
                ),
                key="platforms", source=self.source,
            )
        return getattr(self.platforms, name)

    def accounts_for(self, platform: str) -> Tuple[AccountConfig, ...]:
        return tuple(a for a in self.accounts.values() if platform in a.platforms)
