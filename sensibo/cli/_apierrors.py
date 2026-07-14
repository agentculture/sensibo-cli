"""Map :mod:`sensibo.api`'s :class:`~sensibo.api.ApiError` onto the CLI's
:class:`~sensibo.cli._errors.CliError`.

``sensibo/api/`` is deliberately CLI-independent (``docs/architecture.md``,
"Where the Sensibo code goes"), so it raises its own error type carrying the
same ``code``/``message``/``remediation`` shape rather than importing
:mod:`sensibo.cli`. Every CLI verb that calls through :class:`SensiboClient`
funnels the exception through :func:`from_api_error` here so the top-level
error contract (``CliError`` -> ``_output.emit_error`` -> exit code) still
holds — an ``ApiError`` must never reach ``main()`` unwrapped.

All of :mod:`sensibo.api`'s failure modes (auth, network, rate-limit, gated
history window) stem from the environment the CLI is running in — a missing
key, a network outage, Sensibo throttling — rather than a bad CLI invocation,
so they all map to :data:`sensibo.cli._errors.EXIT_ENV_ERROR`.
"""

from __future__ import annotations

from sensibo.api import ApiError
from sensibo.cli._errors import EXIT_ENV_ERROR, CliError


def from_api_error(err: ApiError) -> CliError:
    """Translate one :class:`ApiError` into the equivalent :class:`CliError`."""
    return CliError(code=EXIT_ENV_ERROR, message=err.message, remediation=err.remediation)
