"""A loopback listener that catches an OAuth ``?code=`` redirect.

Self-hosters should not need to own a domain to authorize an account. Platforms
accept a loopback redirect URI, so the flow is: print the authorize URL, wait on
127.0.0.1 for the browser to come back, read the code out of the query string.

Nothing is exposed to the internet: the socket binds to the loopback interface,
serves exactly one request, and closes.
"""

from __future__ import annotations

import http.server
import threading
import urllib.parse
from typing import Dict, Optional

from makervox_publish.logging import get_logger

log = get_logger(__name__)

_PAGE = (
    "<!doctype html><meta charset=utf-8>"
    "<title>{title}</title>"
    "<body style='font:16px system-ui;margin:3rem;max-width:34rem'>"
    "<h1 style='font-size:1.2rem'>{title}</h1><p>{body}</p>"
    "<p style='color:#666'>You can close this tab and return to the terminal.</p>"
)


class _Handler(http.server.BaseHTTPRequestHandler):
    result: Dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        query = urllib.parse.urlparse(self.path).query
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        type(self).result.update(params)

        if "code" in params:
            title, body = "Authorized", "The authorization code was received."
        else:
            title = "No code received"
            body = ("The platform returned: <code>{0}</code>"
                    .format(params.get("error_description")
                            or params.get("error") or "nothing"))

        page = _PAGE.format(title=title, body=body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *_args) -> None:
        """Silence the default stderr access log; it is noise here."""


def wait_for_code(host: str, port: int, timeout_s: float = 300.0
                  ) -> Optional[Dict[str, str]]:
    """Serve exactly one request and return its query parameters.

    Returns None on timeout. The caller decides what a missing ``code`` means —
    an ``error`` parameter is a legitimate answer, not an exception.
    """
    _Handler.result = {}
    server = http.server.HTTPServer((host, port), _Handler)
    server.timeout = timeout_s
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout_s)
    server.server_close()
    return dict(_Handler.result) or None
