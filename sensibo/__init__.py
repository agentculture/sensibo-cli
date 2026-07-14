"""sensibo-cli — agent-first CLI, and a documented public import surface.

This module is **the** documented entry point for "bigger apps connect via
Python import" (task t10): a third-party script that just wants to control a
Sensibo fleet from Python does ``import sensibo`` and gets a working client,
key resolution, the error family, and the local :class:`~sensibo.store.Store`
— with **zero** argparse or CLI machinery pulled in. See
``docs/api.md`` for the runnable quickstart.

Import-weight contract
-----------------------
``import sensibo`` alone must never import :mod:`argparse` or
:mod:`sensibo.cli`. Everything re-exported here (:mod:`sensibo.api`,
:mod:`sensibo.store`) is, by design, a stdlib-only layer that does not import
from :mod:`sensibo.cli` (see ``docs/architecture.md``, "Where the Sensibo code
goes"). The CLI subpackage is a separate, heavier layer that only loads on an
explicit ``import sensibo.cli`` (or by running the ``sensibo`` console
script) — tests/test_public_api.py guards this via a subprocess
``sys.modules`` check.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from sensibo.api import (
    DEFAULT_BASE_URL,
    ENV_VAR,
    ERROR_AUTH,
    ERROR_GATED,
    ERROR_NETWORK,
    ERROR_RATE_LIMIT,
    ApiError,
    GatedHistoryWindowError,
    HttpError,
    MissingApiKeyError,
    RateLimitExceededError,
    SensiboClient,
    resolve_api_key,
    scrub_text,
    scrub_url,
)
from sensibo.store import (
    DEFAULT_RETENTION_DAYS,
    KIND_POD,
    KIND_ROOM_SENSOR,
    LocationRecord,
    ReadingRecord,
    Store,
    default_db_path,
    derive_unit,
    resolve_db_path,
)

try:
    __version__ = _pkg_version("sensibo-cli")
except PackageNotFoundError:  # pragma: no cover - editable install without metadata
    __version__ = "0.0.0"

#: ``sensibo.Client`` — a short alias for :class:`SensiboClient`. Both names
#: are public; ``Client`` reads well at a call site (``sensibo.Client()``),
#: ``SensiboClient`` is the explicit name used throughout ``sensibo/api/``.
Client = SensiboClient

__all__ = [
    "__version__",
    # -- client ------------------------------------------------------------
    "SensiboClient",
    "Client",
    "DEFAULT_BASE_URL",
    # -- key resolution ------------------------------------------------------
    "ENV_VAR",
    "resolve_api_key",
    # -- errors --------------------------------------------------------------
    "ApiError",
    "MissingApiKeyError",
    "HttpError",
    "RateLimitExceededError",
    "GatedHistoryWindowError",
    "ERROR_AUTH",
    "ERROR_NETWORK",
    "ERROR_RATE_LIMIT",
    "ERROR_GATED",
    # -- scrubbing (never log a URL unscrubbed) -------------------------------
    "scrub_url",
    "scrub_text",
    # -- local store ---------------------------------------------------------
    "Store",
    "LocationRecord",
    "ReadingRecord",
    "DEFAULT_RETENTION_DAYS",
    "KIND_POD",
    "KIND_ROOM_SENSOR",
    "default_db_path",
    "resolve_db_path",
    "derive_unit",
]
