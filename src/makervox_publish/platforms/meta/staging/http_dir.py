"""Stage into a directory you already serve over HTTP. No cloud account needed.

The zero-dependency option: if you already run a web server (or a CDN origin,
or an object store mounted as a directory), copy the file into its web root,
hand Instagram the matching URL, and delete the file afterwards.

Two things this cannot do for you, so both are required with no default:

* ``directory`` — the local web root to write into;
* ``base_url``  — the PUBLIC URL that directory is served at. There is no way to
  derive one from the other, and guessing would produce a URL that 404s only
  when Meta tries to fetch it, minutes into a publish.

The URL must be reachable from Meta's network — not ``localhost``, not a private
address, and not behind basic auth. It is checked for obvious mistakes here,
because the alternative is a publish that fails with an opaque
"media could not be downloaded" several minutes later.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional
from urllib.parse import quote, urlsplit

from makervox_publish.errors import ConfigError, StagingPreconditionError
from makervox_publish.logging import get_logger
from makervox_publish.platforms.meta.staging import StagedMedia, content_type_for

__all__ = ["StaticDirStager"]

log = get_logger(__name__)

_UNREACHABLE_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


class StaticDirStager:
    """Copy into a served directory, publish, delete."""

    def __init__(
        self,
        *,
        directory: str = "",
        base_url: str = "",
        prefix: str = "",
        delete_after_publish: bool = True,
        file_mode: int = 0o644,
        allow_unreachable_host: bool = False,
    ) -> None:
        if not directory:
            raise ConfigError(
                "the static-directory stager needs the local directory your web "
                "server serves from; it has no default.",
                key="platforms.instagram.staging.options.directory",
            )
        if not base_url:
            raise ConfigError(
                "the static-directory stager needs base_url — the PUBLIC URL "
                "that directory is served at. It cannot be derived from the "
                "local path.",
                key="platforms.instagram.staging.options.base_url",
            )
        host = (urlsplit(base_url).hostname or "").lower()
        if host in _UNREACHABLE_HOSTS and not allow_unreachable_host:
            raise ConfigError(
                "base_url points at {0!r}, which Meta's servers cannot reach — "
                "the media has to be fetchable from the public internet. Set "
                "allow_unreachable_host = true only if you are tunnelling that "
                "address somewhere public.".format(host),
                key="platforms.instagram.staging.options.base_url",
            )
        self.directory = os.path.abspath(os.path.expanduser(directory))
        self.base_url = base_url.rstrip("/")
        self.prefix = prefix.strip("/")
        self.delete_after_publish = bool(delete_after_publish)
        self.file_mode = int(file_mode)

    def _target(self, local_path: str) -> str:
        base = os.path.basename(str(local_path))
        return os.path.join(self.prefix, base) if self.prefix else base

    def stage(self, local_path: str, *, content_type: Optional[str] = None) -> StagedMedia:
        relative = self._target(local_path)
        destination = os.path.join(self.directory, relative)
        parent = os.path.dirname(destination)
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copyfile(str(local_path), destination)
            os.chmod(destination, self.file_mode)
        except OSError as exc:
            raise StagingPreconditionError(
                "could not write {0}: {1}. The staging directory must exist and "
                "be writable by whoever runs makervox_publish.".format(destination, exc)
            ) from exc

        url = "{0}/{1}".format(self.base_url, quote(relative))
        log.debug("staged %s as %s (%s)", local_path, url,
                  content_type or content_type_for(local_path))
        return StagedMedia(url=url, key=relative, location=destination, stager=self)

    def unstage(self, staged: StagedMedia) -> None:
        if not self.delete_after_publish:
            log.warning("delete_after_publish is off: %s stays served.", staged)
            return
        os.unlink(os.path.join(self.directory, staged.key))

    def describe(self) -> str:
        return "static directory {0} served at {1}".format(self.directory, self.base_url)
