"""makervox_publish.http — one timeout and retry policy, shared by every platform.

Split out so that "how long do we wait" and "what is worth retrying" are decided
once, in config, instead of being sprinkled as literals through each publisher.
"""

from __future__ import annotations

from makervox_publish.http.client import HttpClient, HttpPolicy, TransportError

__all__ = ["HttpClient", "HttpPolicy", "TransportError"]
