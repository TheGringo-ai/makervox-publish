"""Stage media through a Google Cloud Storage bucket you own.

    pip install "makervox-publish[gcp]"

NO DEFAULTS FOR THE THINGS ONLY YOU KNOW
----------------------------------------
``bucket`` is required and has no default; ``project`` defaults to None so the
client infers it from application-default credentials. The code this was
extracted from hardcoded one person's bucket AND one person's cloud project id,
which is an identity leak and, for anyone who forgot to change it, a
working-looking configuration pointing somewhere they cannot see.

THE TIMEOUT TRAP THIS MODULE EXISTS TO AVOID
--------------------------------------------
Without an explicit ``chunk_size`` the GCS client does a SINGLE-SHOT upload, and
the client's default timeout then covers the WHOLE transfer — so a ~30MB reel on
a slow uplink dies part-way with "Timeout of 120.0s exceeded". That silently cost
three Instagram posts across two accounts before it was understood. Setting
``chunk_size`` switches the client to a RESUMABLE upload where the timeout
applies PER CHUNK, so a slow link stretches the upload instead of failing it.
``chunk_bytes`` must be a multiple of 256 KiB — the API rejects anything else.

THE BUCKET PRECONDITION
-----------------------
``public_mode = "object_acl"`` calls ``blob.make_public()``, which only works on
a bucket with UNIFORM BUCKET-LEVEL ACCESS DISABLED. Modern buckets have UBLA
ENABLED by default, so on a freshly created bucket the happy path fails. That is
detected up front and raised as :class:`StagingPreconditionError` naming the
setting, instead of surfacing a raw 403 that reads like an auth problem.
``public_mode = "signed_url"`` is the alternative and works with UBLA on.
"""

from __future__ import annotations

import datetime
import os
from typing import Any, Optional

from makervox_publish.errors import ConfigError, StagingPreconditionError
from makervox_publish.logging import get_logger
from makervox_publish.platforms.meta.staging import StagedMedia, content_type_for

__all__ = ["GcsStager"]

log = get_logger(__name__)

_CHUNK_MULTIPLE = 256 * 1024


class GcsStager:
    """Upload to a temp object, make just that object fetchable, delete after."""

    def __init__(
        self,
        *,
        bucket: str = "",
        project: Optional[str] = None,
        prefix: str = "makervox_publish-temp",
        public_mode: str = "object_acl",
        signed_url_ttl_s: int = 3600,
        chunk_bytes: int = 8 * 1024 * 1024,
        upload_timeout_s: float = 300.0,
        upload_retry: bool = True,
        delete_after_publish: bool = True,
        client: Optional[Any] = None,
    ) -> None:
        if not bucket:
            raise ConfigError(
                "a staging bucket has no default — only you know which bucket "
                "you own. Set platforms.instagram.staging.options.bucket.",
                key="platforms.instagram.staging.options.bucket",
            )
        if public_mode not in ("object_acl", "signed_url"):
            raise ConfigError(
                "public_mode must be 'object_acl' or 'signed_url', not "
                "{0!r}".format(public_mode),
                key="platforms.instagram.staging.options.public_mode",
            )
        if chunk_bytes % _CHUNK_MULTIPLE:
            raise ConfigError(
                "chunk_bytes must be a multiple of 256 KiB ({0}); {1} is not, "
                "and the API rejects it".format(_CHUNK_MULTIPLE, chunk_bytes),
                key="platforms.instagram.staging.options.chunk_bytes",
            )
        self.bucket_name = bucket
        self.project = project
        self.prefix = prefix.strip("/")
        self.public_mode = public_mode
        self.signed_url_ttl_s = int(signed_url_ttl_s)
        self.chunk_bytes = int(chunk_bytes)
        self.upload_timeout_s = float(upload_timeout_s)
        self.upload_retry = bool(upload_retry)
        self.delete_after_publish = bool(delete_after_publish)
        self._client = client
        self._ubla_checked = False

    # -- client -------------------------------------------------------------- #
    def _storage(self):
        """Import google-cloud-storage lazily.

        Importing makervox_publish must not import a cloud SDK: the extra is optional and
        most self-hosters will never install it.
        """
        try:
            from google.cloud import storage  # noqa: PLC0415 - optional extra
        except ImportError as exc:
            raise ConfigError(
                "the GCS stager needs google-cloud-storage: "
                'pip install "makervox-publish[gcp]"',
                key="platforms.instagram.staging.impl",
            ) from exc
        return storage

    def _bucket(self):
        if self._client is None:
            storage = self._storage()
            self._client = storage.Client(project=self.project)
        return self._client.bucket(self.bucket_name)

    def _check_precondition(self, bucket) -> None:
        """Fail fast, and by name, when the bucket cannot serve public objects."""
        if self.public_mode != "object_acl" or self._ubla_checked:
            return
        self._ubla_checked = True
        try:
            bucket.reload()
            enabled = bool(
                bucket.iam_configuration.uniform_bucket_level_access_enabled
            )
        except Exception as exc:  # noqa: BLE001 - a probe, never the main event
            log.debug("could not read bucket metadata for %s: %s", self.bucket_name, exc)
            return
        if enabled:
            raise StagingPreconditionError(
                "bucket {0!r} has UNIFORM BUCKET-LEVEL ACCESS ENABLED, so "
                "per-object public ACLs cannot be set and Instagram would not "
                "be able to fetch the media. Either disable uniform "
                "bucket-level access on this bucket, or set "
                'public_mode: "signed_url".'.format(self.bucket_name)
            )

    # -- staging ------------------------------------------------------------- #
    def _object_name(self, local_path: str) -> str:
        base = os.path.basename(str(local_path))
        return "{0}/{1}".format(self.prefix, base) if self.prefix else base

    def stage(self, local_path: str, *, content_type: Optional[str] = None) -> StagedMedia:
        bucket = self._bucket()
        self._check_precondition(bucket)

        name = self._object_name(local_path)
        blob = bucket.blob(name)
        # Switches the client from a single-shot upload (whose timeout covers the
        # WHOLE transfer) to a resumable one (timeout per chunk).
        blob.chunk_size = self.chunk_bytes

        kwargs = {
            "content_type": content_type or content_type_for(local_path, "video/mp4"),
            "timeout": self.upload_timeout_s,
        }
        if self.upload_retry:
            # Retrying an upload is safe HERE specifically: the object name is
            # deterministic and the object is deleted right after publish, so a
            # retried chunk overwrites rather than duplicating.
            try:
                from google.cloud.storage.retry import DEFAULT_RETRY

                kwargs["retry"] = DEFAULT_RETRY
            except ImportError:  # pragma: no cover - older client
                pass

        blob.upload_from_filename(str(local_path), **kwargs)
        url = self._publicize(blob)
        return StagedMedia(
            url=url,
            key=name,
            location="gs://{0}/{1}".format(self.bucket_name, name),
            stager=self,
        )

    def _publicize(self, blob) -> str:
        if self.public_mode == "signed_url":
            # Works with uniform bucket-level access ENABLED. Needs a signing
            # identity: a service-account key, or IAM SignBlob permission on the
            # ADC principal.
            return blob.generate_signed_url(
                version="v4",
                expiration=datetime.timedelta(seconds=self.signed_url_ttl_s),
                method="GET",
            )
        try:
            blob.make_public()
        except Exception as exc:  # noqa: BLE001 - re-raised as a named error
            raise StagingPreconditionError(
                "could not make gs://{0}/{1} publicly readable ({2}: {3}). The "
                "usual cause is uniform bucket-level access being ENABLED on the "
                'bucket; disable it, or set public_mode: "signed_url".'.format(
                    self.bucket_name, blob.name, type(exc).__name__, exc
                )
            ) from exc
        return blob.public_url

    def unstage(self, staged: StagedMedia) -> None:
        """Delete the staged object. Raises — the caller logs the leak."""
        if not self.delete_after_publish:
            log.warning(
                "delete_after_publish is off: %s stays publicly readable.", staged
            )
            return
        self._bucket().blob(staged.key).delete()

    def describe(self) -> str:
        return "gcs://{0}/{1} ({2}, {3} KiB chunks)".format(
            self.bucket_name, self.prefix, self.public_mode, self.chunk_bytes // 1024
        )
