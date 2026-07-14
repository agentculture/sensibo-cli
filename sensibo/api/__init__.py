"""``sensibo.api`` — a stdlib-only client for the Sensibo cloud API v2.

Usable as a standalone library: this package does not import anything from
:mod:`sensibo.cli`, so a third-party script can ``import sensibo.api`` without
pulling in argparse or any CLI machinery (``docs/architecture.md``, "Where the
Sensibo code goes"). The CLI verbs that will eventually call this client live
in ``sensibo/cli/_commands/`` and are a separate, later layer.

Quick example::

    from sensibo.api import SensiboClient

    client = SensiboClient()  # reads SENSIBO_API_KEY, then ~/.sensibo/.env
    fleet = client.fleet_snapshot()  # ONE call: GET /users/me/pods?fields=*
"""

from __future__ import annotations

from sensibo.api._auth import ENV_VAR, resolve_api_key
from sensibo.api._errors import (
    ERROR_AUTH,
    ERROR_GATED,
    ERROR_NETWORK,
    ERROR_RATE_LIMIT,
    ApiError,
    GatedHistoryWindowError,
    HttpError,
    MissingApiKeyError,
    RateLimitExceededError,
)
from sensibo.api._scrub import scrub_text, scrub_url
from sensibo.api.client import DEFAULT_BASE_URL, SensiboClient

__all__ = [
    "SensiboClient",
    "DEFAULT_BASE_URL",
    "ENV_VAR",
    "resolve_api_key",
    "ApiError",
    "MissingApiKeyError",
    "HttpError",
    "RateLimitExceededError",
    "GatedHistoryWindowError",
    "ERROR_AUTH",
    "ERROR_NETWORK",
    "ERROR_RATE_LIMIT",
    "ERROR_GATED",
    "scrub_url",
    "scrub_text",
]
