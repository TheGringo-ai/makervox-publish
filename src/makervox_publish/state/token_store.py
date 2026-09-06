"""Where OAuth tokens live, and how two processes are stopped from destroying one.

THE DEFAULT IS A LOCAL FILE
---------------------------
:class:`FileTokenStore` — atomic temp+replace, owner-only, one named lock — is
the zero-config default. The code this was extracted from defaulted to a CLOUD
secret backend, so a fresh install shelled out to a CLI most machines do not
have and paid roughly a second of failed subprocess on EVERY API call before
falling back to the file. Inverting that default is the single biggest usability
change in the extraction. The cloud path survives as
:class:`MirroredTokenStore`, opt-in.

THE REFRESH-ROTATION RACE, PROMOTED FROM A COMMENT TO AN API
------------------------------------------------------------
TikTok **rotates the refresh token on use**: a successful refresh SPENDS the old
one. Two refreshes of the same account race destructively — the loser presents a
token the platform has already retired, its call fails, and that account needs a
MANUAL re-authorization. Nothing recovers it.

:meth:`TokenStore.transaction` is the defence, and it does three things in this
order:

1. acquire the store's named lock, so overlapping jobs on this host serialize;
2. drop the in-process cache — never refresh against a stale snapshot;
3. **re-read inside the lock**, so a caller that lost the race sees the winner's
   freshly rotated token and ADOPTS it instead of spending its own.

Step 3 is what makes the lock worth having. A lock that only serializes still
lets caller #2 spend a token that caller #1 already replaced.

The cross-machine leg (two hosts cannot share a flock) is deliberately NOT in
here: it is ``invalidate()`` + ``load()`` after a rejected refresh, driven by
the platform client, so a token another machine just rotated gets adopted rather
than failing the publish.

WRITE ORDER IN THE MIRROR IS NOT CONFIGURABLE
---------------------------------------------
Local disk first, then remote, and the remote write NEVER raises. A freshly
rotated refresh token is unrecoverable if lost, so it lands on a local disk
before anything that can time out, and a remote outage must not eat a token that
was just minted.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional

from makervox_publish.errors import ConfigError, MakervoxPublishError
from makervox_publish.logging import get_logger, swallowed
from makervox_publish.state.atomic import read_json, write_json
from makervox_publish.state.locks import file_lock
from makervox_publish.state.paths import expand

try:  # pragma: no cover - typing only
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - Python < 3.8
    Protocol = object  # type: ignore[assignment]

    def runtime_checkable(cls):  # type: ignore[misc]
        return cls

__all__ = [
    "TokenStore",
    "TokenTransaction",
    "Transaction",
    "LockSettings",
    "FileTokenStore",
    "SecretManagerTokenStore",
    "MirroredTokenStore",
    "build_token_store",
]

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# lock settings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LockSettings:
    """The ``[state.locks]`` block, in the shape a store needs it.

    Passed down rather than read from a global so a test (or a second config in
    one process) cannot accidentally share one host's lock directory.
    """

    dir: str
    backend: str = "auto"
    timeout_s: float = 120.0
    on_unavailable: str = "proceed"

    @classmethod
    def from_config(cls, state_cfg) -> "LockSettings":
        locks = getattr(state_cfg, "locks", None)
        state_dir = expand(getattr(state_cfg, "dir", "~/.local/state/makervox_publish"))
        return cls(
            dir=expand(getattr(locks, "dir", "") or os.path.join(state_dir, "locks")),
            backend=getattr(locks, "backend", "auto"),
            timeout_s=float(getattr(locks, "acquire_timeout_s", 120.0)),
            on_unavailable=getattr(locks, "on_unavailable", "proceed"),
        )


# --------------------------------------------------------------------------- #
# interface
# --------------------------------------------------------------------------- #
@runtime_checkable
class TokenStore(Protocol):
    """A named blob of tokens: ``{<account>: {...}}`` for most platforms."""

    name: str

    def load(self) -> Dict[str, Any]:
        """Current contents. Returns ``{}`` when nothing is stored yet."""
        ...

    def save(self, data: Dict[str, Any]) -> None:
        """Replace the contents. Must be atomic and owner-only."""
        ...

    def invalidate(self) -> None:
        """Drop any in-process cache, so the next ``load()`` really re-reads."""
        ...

    def transaction(self, *, timeout_s: Optional[float] = None):
        """Lock, invalidate, re-read, yield a :class:`TokenTransaction`."""
        ...

    def describe(self) -> str:
        """One line for ``makervox_publish doctor``. Never a token value."""
        ...


class TokenTransaction:
    """The state seen INSIDE the lock, plus the way to write it back.

    ``data`` is a fresh read taken AFTER the lock was acquired. Code that
    refreshes a rotating token MUST look at ``data`` and not at whatever it read
    before it started waiting — that stale snapshot is the whole bug.

    ``held`` reports whether the lock was actually taken. Under
    ``state.locks.on_unavailable = "proceed"`` it can be False, and a caller that
    cares (a refresh does) can say so in its logs.
    """

    __slots__ = ("data", "held", "_store", "_saved")

    def __init__(self, store: Any, data: Dict[str, Any], held: bool) -> None:
        self.data = data
        self.held = held
        self._store = store
        self._saved = False

    def save(self, data: Optional[Dict[str, Any]] = None) -> None:
        if data is not None:
            self.data = data
        self._store.save(self.data)
        self._saved = True

    @property
    def saved(self) -> bool:
        return self._saved


#: The name the rest of the package uses when the store is obvious from context.
Transaction = TokenTransaction


class _CachedStore:
    """Shared TTL cache.

    Meaningful only for stores whose read costs something. A remote read of
    roughly a second, on a code path that runs before EVERY API request, is why
    this exists at all — an uncached remote store adds that second to every
    single call.
    """

    def __init__(self, cache_ttl_s: float = 0.0) -> None:
        self._cache_ttl_s = max(float(cache_ttl_s), 0.0)
        self._cache_at = 0.0
        self._cache_val = None  # type: Optional[Dict[str, Any]]

    def _cached(self) -> Optional[Dict[str, Any]]:
        if self._cache_val is None or self._cache_ttl_s <= 0:
            return None
        if time.time() - self._cache_at >= self._cache_ttl_s:
            return None
        return self._cache_val

    def _remember(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self._cache_at = time.time()
        self._cache_val = data
        return data

    def invalidate(self) -> None:
        self._cache_at = 0.0
        self._cache_val = None


@contextlib.contextmanager
def _transaction(store: Any, locks: Optional[LockSettings], lock_name: str,
                 timeout_s: Optional[float]) -> Iterator[TokenTransaction]:
    """The lock -> invalidate -> re-read sequence, shared by every store."""
    if locks is None:
        # No lock configured for this store. Say so: an unlocked refresh can
        # permanently spend a rotating refresh token.
        log.warning("token store %r has no lock configured — refreshing UNLOCKED",
                    getattr(store, "name", lock_name))
        store.invalidate()
        yield TokenTransaction(store, store.load(), False)
        return
    with file_lock(lock_name, locks.dir, backend=locks.backend,
                   timeout_s=locks.timeout_s if timeout_s is None else float(timeout_s),
                   on_unavailable=locks.on_unavailable) as lock:
        store.invalidate()          # never act on a cached snapshot
        yield TokenTransaction(store, store.load(), bool(lock.held))


# --------------------------------------------------------------------------- #
# the default: a local file
# --------------------------------------------------------------------------- #
class FileTokenStore(_CachedStore):
    """A JSON file, written atomically, owner-only, behind a named lock.

    ``lock`` is a NAME, not a path: two stores sharing a name share a lock. That
    is deliberate — Facebook and Instagram share one Meta token file, and giving
    them one lock is what stops a read-modify-write from losing a token the
    other just refreshed.
    """

    def __init__(
        self,
        path: str,
        *,
        atomic_write: bool = True,
        lock: str = "",
        locks: Optional[LockSettings] = None,
        file_mode: int = 0o600,
        dir_mode: int = 0o700,
        enforce_file_mode: bool = True,
        cache_ttl_s: float = 0.0,
    ) -> None:
        _CachedStore.__init__(self, cache_ttl_s)
        self.path = expand(path)
        self.atomic_write = bool(atomic_write)
        self.lock_name = lock or os.path.splitext(os.path.basename(self.path))[0]
        self.locks = locks
        self.file_mode = int(file_mode)
        self.dir_mode = int(dir_mode)
        self.enforce_file_mode = bool(enforce_file_mode)
        self.name = "file:{0}".format(self.path)

    # -- reading/writing ----------------------------------------------------- #
    def load(self) -> Dict[str, Any]:
        hit = self._cached()
        if hit is not None:
            return hit
        data = read_json(self.path, default={})
        if not isinstance(data, dict):
            log.warning("token store %s does not contain a JSON object; ignoring it",
                        self.path)
            data = {}
        return self._remember(data)

    def save(self, data: Dict[str, Any]) -> None:
        if self.atomic_write:
            write_json(
                self.path, data,
                file_mode=self.file_mode,
                dir_mode=self.dir_mode,
                enforce_file_mode=self.enforce_file_mode,
            )
        else:
            # Offered only because some network filesystems refuse rename-over.
            # It is strictly worse: a crash mid-write leaves an EMPTY token file
            # and, for a rotated refresh token, nothing to recover from.
            from makervox_publish.state.paths import chmod_quietly, ensure_parent

            target = ensure_parent(self.path, self.dir_mode)
            with open(target, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=1, ensure_ascii=False)
            chmod_quietly(target, self.file_mode, enforce=self.enforce_file_mode)
        self._remember(data)

    def transaction(self, *, timeout_s: Optional[float] = None):
        return _transaction(self, self.locks, self.lock_name, timeout_s)

    def describe(self) -> str:
        return "local file {0} (lock {1!r}, mode {2:o})".format(
            self.path, self.lock_name, self.file_mode
        )

    def __repr__(self) -> str:
        return "<FileTokenStore {0}>".format(self.path)


# --------------------------------------------------------------------------- #
# opt-in: a cloud secret as the shared source of truth
# --------------------------------------------------------------------------- #
class SecretManagerTokenStore(_CachedStore):
    """GCP Secret Manager. NEVER a default, and there is NO default project id.

    Two backends, because both are legitimately useful:

    ``use_cli = false``
        the ``google-cloud-secret-manager`` library (``pip install makervox-publish[gcp]``)
    ``use_cli = true``
        shell out to ``gcloud``, for hosts that already have an authenticated
        CLI and would rather not add the dependency. (The code this came from
        chose the CLI deliberately for exactly that reason — one fewer
        dependency to keep in sync with an already-proven binary.)

    Version pruning is not optional housekeeping: Secret Manager bills per
    ENABLED VERSION PER MONTH, and a refresh token that rotates daily is roughly
    365 paid versions a year for ONE file. Keep a few for rollback, destroy the
    rest — and never let pruning fail a write.
    """

    def __init__(
        self,
        *,
        project: str,
        secret_id: str,
        use_cli: bool = False,
        gcloud_path: str = "gcloud",
        timeout_s: float = 30.0,
        keep_versions: int = 3,
        cache_ttl_s: float = 60.0,
        locks: Optional[LockSettings] = None,
        lock: str = "",
        **_ignored: Any,
    ) -> None:
        _CachedStore.__init__(self, cache_ttl_s)
        if not project:
            raise ConfigError(
                "SecretManagerTokenStore has no default project id — only you "
                "know which cloud project holds your tokens.",
                key="token_stores.*.options.project",
            )
        if not secret_id:
            raise ConfigError(
                "SecretManagerTokenStore needs the secret id that holds the "
                "token blob.",
                key="token_stores.*.options.secret_id",
            )
        self.project = project
        self.secret_id = secret_id
        self.use_cli = bool(use_cli)
        self.gcloud_path = gcloud_path
        self.timeout_s = float(timeout_s)
        self.keep_versions = max(int(keep_versions), 1)
        self.locks = locks
        self.lock_name = lock or "secretmanager-" + secret_id
        self.name = "gcp-secret:{0}/{1}".format(project, secret_id)
        self._client = None
        if not self.use_cli:
            self._client = self._build_client()

    # -- backends ------------------------------------------------------------ #
    def _build_client(self):
        try:
            from google.cloud import secretmanager  # type: ignore[import-not-found]
        except ImportError as exc:
            # A startup error, not an intermittent one: fail here rather than
            # halfway through a publish.
            raise ConfigError(
                "SecretManagerTokenStore needs the google-cloud-secret-manager "
                "library: pip install 'makervox-publish[gcp]'. Or set use_cli = true to "
                "shell out to an already-authenticated gcloud instead.",
                key="token_stores.*.options.use_cli",
            ) from exc
        return secretmanager.SecretManagerServiceClient()

    def _gcloud(self, args, stdin: Optional[str] = None):
        return subprocess.run(  # noqa: S603 - argv list, no shell
            [self.gcloud_path] + list(args),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
        )

    # -- reading/writing ----------------------------------------------------- #
    def load(self) -> Dict[str, Any]:
        hit = self._cached()
        if hit is not None:
            return hit
        raw = self._read_raw()
        if raw is None:
            # Do NOT cache a failed read as "{}": that would mask an outage as
            # "no tokens stored" for the whole TTL and send the caller down the
            # re-authorize path for a transient network problem.
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            log.warning("secret %s does not hold JSON; treating it as empty", self.name)
            return {}
        if not isinstance(data, dict):
            return {}
        return self._remember(data)

    def _read_raw(self) -> Optional[str]:
        if self.use_cli:
            try:
                proc = self._gcloud([
                    "secrets", "versions", "access", "latest",
                    "--secret=" + self.secret_id, "--project=" + self.project,
                ])
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("secret read failed for %s: %s", self.name, exc)
                return None
            if proc.returncode != 0:
                log.warning("secret read failed for %s: %s",
                            self.name, (proc.stderr or "").strip()[:200])
                return None
            return proc.stdout
        try:
            name = "projects/{0}/secrets/{1}/versions/latest".format(
                self.project, self.secret_id
            )
            resp = self._client.access_secret_version(request={"name": name})
            return resp.payload.data.decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - any cloud error is "unavailable"
            log.warning("secret read failed for %s: %s: %s",
                        self.name, type(exc).__name__, exc)
            return None

    def save(self, data: Dict[str, Any]) -> None:
        """Add a version, then prune. Returns normally even on failure.

        The caller has already persisted this token somewhere durable (the
        mirror writes local first, by design), so a cloud outage here must not
        raise and abort a publish that has otherwise succeeded.
        """
        payload = json.dumps(data, indent=1, ensure_ascii=False)
        if not self._write_raw(payload):
            return
        self._remember(data)
        try:
            self._prune()
        except Exception as exc:  # noqa: BLE001
            swallowed(log, "secret version pruning", exc,
                      detail="billing keeps accruing for the extra enabled versions")

    def _write_raw(self, payload: str) -> bool:
        if self.use_cli:
            try:
                proc = self._gcloud([
                    "secrets", "versions", "add", self.secret_id,
                    "--project=" + self.project, "--data-file=-",
                ], stdin=payload)
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("secret write failed for %s (the local copy is still "
                            "authoritative): %s", self.name, exc)
                return False
            if proc.returncode != 0:
                log.warning("secret write failed for %s (the local copy is still "
                            "authoritative): %s",
                            self.name, (proc.stderr or "").strip()[:200])
                return False
            return True
        try:
            parent = "projects/{0}/secrets/{1}".format(self.project, self.secret_id)
            self._client.add_secret_version(
                request={"parent": parent,
                         "payload": {"data": payload.encode("utf-8")}}
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("secret write failed for %s (the local copy is still "
                        "authoritative): %s: %s", self.name, type(exc).__name__, exc)
            return False

    def _prune(self) -> None:
        """Destroy every enabled version past ``keep_versions``."""
        if self.use_cli:
            listing = self._gcloud([
                "secrets", "versions", "list", self.secret_id,
                "--project=" + self.project, "--filter=state:ENABLED",
                "--format=value(name)", "--sort-by=~name",
            ])
            if listing.returncode != 0:
                return
            stale = [v for v in listing.stdout.split() if v][self.keep_versions:]
            for version in stale:
                self._gcloud([
                    "secrets", "versions", "destroy", version,
                    "--secret=" + self.secret_id, "--project=" + self.project,
                    "--quiet",
                ])
            return
        parent = "projects/{0}/secrets/{1}".format(self.project, self.secret_id)
        enabled = [
            v for v in self._client.list_secret_versions(request={"parent": parent})
            if getattr(v.state, "name", str(v.state)) == "ENABLED"
        ]
        enabled.sort(key=lambda v: int(str(v.name).rsplit("/", 1)[-1]), reverse=True)
        for version in enabled[self.keep_versions:]:
            self._client.destroy_secret_version(request={"name": version.name})

    def transaction(self, *, timeout_s: Optional[float] = None):
        return _transaction(self, self.locks, self.lock_name, timeout_s)

    def describe(self) -> str:
        return "GCP Secret Manager {0} (via {1}, keep {2} versions)".format(
            self.name, "gcloud CLI" if self.use_cli else "client library",
            self.keep_versions,
        )

    def __repr__(self) -> str:
        return "<SecretManagerTokenStore {0}>".format(self.name)


# --------------------------------------------------------------------------- #
# opt-in: remote source of truth, local cache and offline fallback
# --------------------------------------------------------------------------- #
class MirroredTokenStore(_CachedStore):
    """A remote store as the source of truth, with a local file as the cache.

    Two properties are LOAD-BEARING and neither is configurable:

    **Write order is local, then remote.** A freshly rotated refresh token is
    unrecoverable if lost — the previous one is already spent — so it lands on
    local disk before anything that can time out.

    **The remote write never raises.** A cloud outage must not lose a token that
    was just minted, and it must not turn a successful publish into a failure.

    The offline fallback on READ is load-bearing too: a scheduled publish must
    not die because a remote secret read timed out. That is what
    ``fallback_to_local_on_error`` protects, and turning it off means accepting
    that a network blip drops posts.
    """

    def __init__(
        self,
        *,
        local: Any,
        remote: Any,
        fallback_to_local_on_error: bool = True,
        locks: Optional[LockSettings] = None,
        lock: str = "",
        cache_ttl_s: float = 60.0,
        **plugin_extras: Any,
    ) -> None:
        _CachedStore.__init__(self, cache_ttl_s)
        self.local = _coerce_store(local, "local", locks=locks, **plugin_extras)
        self.remote = _coerce_store(remote, "remote", locks=locks, **plugin_extras)
        self.fallback_to_local_on_error = bool(fallback_to_local_on_error)
        self.locks = locks
        self.lock_name = lock or getattr(self.local, "lock_name", "mirrored-tokens")
        self.name = "mirror({0} <- {1})".format(
            getattr(self.local, "name", "local"), getattr(self.remote, "name", "remote")
        )

    def load(self) -> Dict[str, Any]:
        hit = self._cached()
        if hit is not None:
            return hit
        try:
            remote = self.remote.load()
        except Exception as exc:  # noqa: BLE001
            swallowed(log, "remote token read", exc,
                      detail="falling back to the local copy")
            remote = {}
        if remote:
            return self._remember(remote)
        if not self.fallback_to_local_on_error:
            return self._remember({})
        return self._remember(self.local.load())

    def save(self, data: Dict[str, Any]) -> None:
        # ORDER IS NOT CONFIGURABLE. Local disk first.
        self.local.save(data)
        self._remember(data)
        try:
            self.remote.save(data)
        except Exception as exc:  # noqa: BLE001
            swallowed(log, "remote token mirror", exc,
                      detail="the local copy is authoritative; other machines "
                             "will not see this rotation until the next write")

    def invalidate(self) -> None:
        _CachedStore.invalidate(self)
        self.local.invalidate()
        self.remote.invalidate()

    def transaction(self, *, timeout_s: Optional[float] = None):
        return _transaction(self, self.locks, self.lock_name, timeout_s)

    def describe(self) -> str:
        return "{0} mirrored to {1}{2}".format(
            self.local.describe(), self.remote.describe(),
            "" if self.fallback_to_local_on_error else " (NO offline fallback)",
        )

    def __repr__(self) -> str:
        return "<MirroredTokenStore {0}>".format(self.name)


# --------------------------------------------------------------------------- #
# construction from config
# --------------------------------------------------------------------------- #
def _coerce_store(spec: Any, role: str, **extras: Any):
    """Accept an already-built store, or a ``{"impl", "options"}`` mapping."""
    if spec is None:
        raise ConfigError(
            "MirroredTokenStore needs a {0!r} store".format(role),
            key="token_stores.*.options." + role,
        )
    if hasattr(spec, "load") and hasattr(spec, "save"):
        return spec
    if isinstance(spec, dict):
        from makervox_publish.config.plugins import instantiate

        options = dict(spec.get("options") or {})
        for key, value in extras.items():
            options.setdefault(key, value)
        return instantiate(spec.get("impl"), options,
                           key="token_stores.*.options." + role,
                           group="makervox_publish.token_stores")
    raise ConfigError(
        "{0!r} must be a token store or an {{impl, options}} table, not {1}".format(
            role, type(spec).__name__
        ),
        key="token_stores.*.options." + role,
    )


def build_token_store(spec, state_cfg, *, cache_ttl_s: Optional[float] = None):
    """Instantiate a configured token store with the state-wide defaults applied.

    ``spec`` is a :class:`makervox_publish.config.PluginSpec` from ``[token_stores.<name>]``.
    Permissions, the lock settings and the cache TTL come from the state block,
    so every store on a host agrees about them.
    """
    extras = {
        "locks": LockSettings.from_config(state_cfg),
        "file_mode": int(getattr(state_cfg, "file_mode", 0o600)),
        "dir_mode": int(getattr(state_cfg, "dir_mode", 0o700)),
        "enforce_file_mode": bool(getattr(state_cfg, "enforce_file_mode", True)),
    }
    if cache_ttl_s is not None:
        extras["cache_ttl_s"] = float(cache_ttl_s)
    try:
        return spec.build(group="makervox_publish.token_stores", **extras)
    except MakervoxPublishError:
        raise
    except TypeError as exc:  # a third-party store with a narrower signature
        raise ConfigError(
            "cannot construct token store {0!r}: {1}".format(
                getattr(spec, "impl", spec), exc
            ),
            key=getattr(spec, "key", None) or "token_stores",
        ) from exc
