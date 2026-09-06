"""SecretManagerProvider — GCP Secret Manager. NEVER a default.

There is NO default project id: defaulting it to the author's own project is
simultaneously an identity leak and a silent-misconfiguration trap.

Two backends:
    use_cli=False -> google-cloud-secret-manager   (pip install 'makervox-publish[gcp]')
    use_cli=True  -> subprocess `gcloud secrets versions access latest ...`
                     for hosts that already have an authenticated CLI and would
                     rather not add the library dependency.

``probe_existence=True`` runs ONE listing and caches the names, so absent keys
cost nothing. The alternative — a subprocess per missing key — costs about a
second each, which is how "this platform is not configured" turns into a stall
on every single call.
"""

from __future__ import annotations

import json
import subprocess
from typing import Callable, Optional, Set

from makervox_publish.credentials.base import ProviderUnavailable
from makervox_publish.logging import get_logger

__all__ = ["SecretManagerProvider"]

log = get_logger(__name__)


class SecretManagerProvider:
    def __init__(
        self,
        *,
        project: str,                      # required; no default, ever
        use_cli: bool = False,
        gcloud_path: str = "gcloud",
        timeout_s: float = 20.0,
        probe_existence: bool = True,
        name_transform: Optional[Callable[[str], str]] = None,
    ) -> None:
        if not project:
            raise ProviderUnavailable(
                "SecretManagerProvider needs an explicit `project`; there is no "
                "default GCP project id."
            )
        self.project = project
        self.use_cli = bool(use_cli)
        self.gcloud_path = gcloud_path
        self.timeout_s = float(timeout_s)
        self.probe_existence = bool(probe_existence)
        self._transform = name_transform or (lambda key: key)
        self.name = "gcp_secret_manager({0})".format(project)
        self._client = None
        self._names = None  # type: Optional[Set[str]]

        if not self.use_cli:
            try:
                from google.cloud import secretmanager  # noqa: WPS433 - optional extra
            except ImportError as exc:
                raise ProviderUnavailable(
                    "the gcp extra is not installed: pip install 'makervox-publish[gcp]' "
                    "(or set use_cli = true to shell out to gcloud instead)"
                ) from exc
            try:
                self._client = secretmanager.SecretManagerServiceClient()
            except Exception as exc:
                raise ProviderUnavailable(
                    "cannot create a Secret Manager client (no application "
                    "default credentials?): {0}".format(exc)
                ) from exc

    # -- listing (one round trip, not one per key) -------------------------- #
    def _load_names(self) -> Set[str]:
        if self._names is not None:
            return self._names
        names = set()  # type: Set[str]
        try:
            if self._client is not None:
                parent = "projects/{0}".format(self.project)
                for secret in self._client.list_secrets(request={"parent": parent}):
                    names.add(secret.name.rsplit("/", 1)[-1])
            else:
                out = self._run_gcloud(
                    ["secrets", "list", "--project", self.project,
                     "--format", "json(name)"]
                )
                for item in json.loads(out or "[]"):
                    raw = item.get("name", "")
                    if raw:
                        names.add(raw.rsplit("/", 1)[-1])
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable(
                "cannot list secrets in project {0}: {1}".format(self.project, exc)
            ) from exc
        self._names = names
        return names

    def _run_gcloud(self, args) -> str:
        argv = [self.gcloud_path] + list(args)
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, no shell
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_s,
            )
        except FileNotFoundError as exc:
            raise ProviderUnavailable(
                "gcloud not found at {0!r}".format(self.gcloud_path)
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderUnavailable(
                "gcloud timed out after {0}s".format(self.timeout_s)
            ) from exc
        if proc.returncode != 0:
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-400:]
            raise ProviderUnavailable("gcloud failed: {0}".format(tail))
        return (proc.stdout or b"").decode("utf-8", "replace")

    # -- provider interface ------------------------------------------------- #
    def get(self, key: str) -> Optional[str]:
        secret_id = self._transform(key)
        if self.probe_existence and secret_id not in self._load_names():
            return None
        if self._client is not None:
            name = "projects/{0}/secrets/{1}/versions/latest".format(self.project, secret_id)
            try:
                resp = self._client.access_secret_version(request={"name": name})
            except Exception as exc:
                if "NotFound" in type(exc).__name__ or "404" in str(exc):
                    return None
                raise ProviderUnavailable(
                    "cannot access secret {0}: {1}".format(secret_id, exc)
                ) from exc
            return (resp.payload.data.decode("utf-8") or "").strip() or None

        out = self._run_gcloud(
            ["secrets", "versions", "access", "latest",
             "--secret", secret_id, "--project", self.project]
        )
        return (out or "").strip() or None

    def has(self, key: str) -> bool:
        if self.probe_existence:
            return self._transform(key) in self._load_names()
        return self.get(key) is not None

    def describe(self) -> str:
        how = "gcloud CLI" if self.use_cli else "google-cloud-secret-manager"
        return "GCP Secret Manager project={0} via {1}".format(self.project, how)

    def __repr__(self) -> str:
        return "<SecretManagerProvider {0}>".format(self.project)
