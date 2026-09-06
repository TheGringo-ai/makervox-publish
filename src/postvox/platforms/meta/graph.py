"""One Graph session. ONE config key drives BOTH hosts.

Meta publishing talks to two hosts that must stay in lockstep:

* ``https://graph.facebook.com/<version>`` — everything except uploaded bytes;
* ``https://rupload.facebook.com/video-upload/<version>`` — the resumable
  upload endpoint, which takes RAW BODY BYTES (not multipart) and an
  ``Authorization: OAuth <token>`` header rather than an ``access_token`` field.

In the original they were two string literals each carrying ``v21.0``, so a
version bump could update one and leave the other behind. Here ``api_version``
is one setting and both URLs are derived from it.

Nothing in this module raises on an HTTP status: Graph puts the actionable half
of a failure in the JSON body, so every call returns the decoded body and the
caller decides. The version-sensitive readings of those bodies all live in
:mod:`postvox.platforms.meta.compat`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from postvox.http.client import HttpClient
from postvox.logging import get_logger
from postvox.platforms.meta import compat

__all__ = ["GraphEndpoints", "GraphSession"]

log = get_logger(__name__)


@dataclass(frozen=True)
class GraphEndpoints:
    """Both hosts, derived from one version."""

    api_version: str = "v21.0"
    graph_base: str = "https://graph.facebook.com"
    rupload_base: str = "https://rupload.facebook.com/video-upload"

    @classmethod
    def from_config(cls, platform_cfg) -> "GraphEndpoints":
        """Build from :class:`postvox.config.FacebookConfig` (or Instagram's)."""
        return cls(
            api_version=str(getattr(platform_cfg, "api_version", None) or "v21.0"),
            graph_base=str(getattr(platform_cfg, "graph_base",
                                   "https://graph.facebook.com")).rstrip("/"),
            rupload_base=str(getattr(platform_cfg, "rupload_base",
                                     "https://rupload.facebook.com/video-upload")).rstrip("/"),
        )

    @property
    def graph(self) -> str:
        return "{0}/{1}".format(self.graph_base.rstrip("/"), self.api_version)

    @property
    def rupload(self) -> str:
        return "{0}/{1}".format(self.rupload_base.rstrip("/"), self.api_version)

    def node(self, path: str) -> str:
        return "{0}/{1}".format(self.graph, str(path).lstrip("/"))

    def upload(self, video_id: str) -> str:
        return "{0}/{1}".format(self.rupload, video_id)


class GraphSession:
    """Thin, token-per-call wrapper over :class:`postvox.http.HttpClient`.

    The token is a per-CALL argument, not session state: one process publishes
    for several accounts, each with its own Page token, and a session that
    remembered one of them is a cross-account posting bug waiting to happen.
    """

    def __init__(self, http: HttpClient, endpoints: GraphEndpoints,
                 error_chars: int = 400) -> None:
        self.http = http
        self.endpoints = endpoints
        self.error_chars = int(error_chars)

    # -- reads ---------------------------------------------------------------- #
    def get(self, path: str, token: Optional[str], params: Optional[Dict[str, Any]] = None,
            *, kind: str = "default", attempts: Optional[int] = None) -> Dict[str, Any]:
        query = dict(params or {})
        if token:
            # An empty access_token parameter is not the same as no parameter:
            # the token exchange carries its credentials in the query itself.
            query["access_token"] = token
        response = self.http.get(self.endpoints.node(path), params=query, kind=kind,
                                 attempts=attempts)
        return self.http.json_body(response)

    # -- writes --------------------------------------------------------------- #
    def post(self, path: str, token: str, data: Optional[Dict[str, Any]] = None, *,
             files: Optional[Dict[str, Any]] = None, kind: str = "small_write",
             attempts: int = 1) -> Dict[str, Any]:
        """POST to a Graph node.

        ``attempts`` defaults to 1: a POST that CREATES something must not be
        retried blindly. A timed-out create may already have landed, and the
        retry then makes a second one. Callers that have made a specific POST
        idempotent (by reusing an already-minted upload id) opt in explicitly.
        """
        body = dict(data or {})
        body["access_token"] = token
        response = self.http.post(self.endpoints.node(path), data=body, files=files,
                                  kind=kind, attempts=attempts)
        return self.http.json_body(response)

    # -- resumable upload host ------------------------------------------------ #
    def upload_bytes(self, video_id: str, token: str, payload: bytes, *,
                     offset: int = 0, file_size: Optional[int] = None,
                     attempts: int = 1) -> Dict[str, Any]:
        """Send raw bytes to the resumable-upload host for one video id.

        Not multipart: the body IS the file, and the credentials ride in an
        ``Authorization: OAuth`` header. Re-sending the same bytes to the same
        video id at offset 0 is idempotent on the platform's side — a timeout
        whose request actually landed is deduped, not duplicated — which is what
        makes retrying THIS call safe when retrying the whole publish is not.
        """
        size = len(payload) if file_size is None else int(file_size)
        response = self.http.post(
            self.endpoints.upload(video_id),
            data=payload,
            headers={
                "Authorization": "OAuth {0}".format(token),
                "offset": str(int(offset)),
                "file_size": str(size),
            },
            kind="upload_chunk",
            attempts=attempts,
        )
        return self.http.json_body(response)

    # -- diagnostics ---------------------------------------------------------- #
    def error(self, payload: Any) -> str:
        return compat.error_message(payload, self.error_chars)

    def failed(self, payload: Any) -> bool:
        return compat.graph_error(payload) is not None

    def describe(self) -> str:
        return "Graph {0} ({1}, uploads via {2})".format(
            self.endpoints.api_version, self.endpoints.graph_base,
            self.endpoints.rupload_base,
        )
