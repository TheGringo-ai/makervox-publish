"""postvox.platforms.x — the X (Twitter) publisher and its posting governor.

Two modules, split along the line that matters:

* :mod:`postvox.platforms.x.client` talks to the API.
* :mod:`postvox.platforms.x.governor` decides whether it may — a daily cap, two
  allow-lists and near-duplicate suppression, backed by a
  :class:`~postvox.state.counters.CounterStore`.

The governor is separate because it is the part that must be reviewed and
tested. Suppression on this platform is ONE-WAY (an account that reads as a
marketing bot cannot post its way back into distribution) and posting is
pay-per-call, so every rule it enforces is a deliberate trade of reach per post
against the account staying visible and the bill staying small.

Importing this package pulls in ``requests`` (via the shared HTTP layer) but not
``requests_oauthlib``: OAuth 1.0a signing is the ``postvox[x]`` extra, and its
absence is reported as a "not configured" result rather than an ImportError.
"""

from __future__ import annotations

from postvox.platforms.x.client import X_CREDENTIALS, XClient, credential_spec, shape
from postvox.platforms.x.governor import Governor, GovernorDecision

__all__ = [
    "XClient",
    "X_CREDENTIALS",
    "credential_spec",
    "shape",
    "Governor",
    "GovernorDecision",
]
