"""Shared helper: build a :class:`SensiboClient` and map ``ApiError`` -> ``CliError``.

``sensibo.api`` is a standalone library that never imports ``sensibo.cli``
(``docs/architecture.md``, "Where the Sensibo code goes"), so the CLI layer is
where the two error shapes meet. Every verb that talks to the Sensibo cloud
(``devices`` and ``read`` today; ``set``/``collect``/``query``/... in later
tasks) needs the same two things: construct a client (key resolution can
itself raise ``MissingApiKeyError``, a subclass of ``ApiError``) and translate
any ``ApiError`` the client raises into a ``CliError`` carrying the same
``code``/``message``/``remediation`` — otherwise it would fall through
``_dispatch``'s generic exception wrapper and lose its remediation hint.
"""

from __future__ import annotations

from typing import Callable

from sensibo.api import ApiError, HttpError, SensiboClient
from sensibo.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

_USER_HTTP_STATUSES = {400, 404}


def _exit_code(err: ApiError) -> int:
    """Map an API-layer error category onto the CLI exit-code contract.

    ``ApiError.code`` is a *category* (auth=2, network=3, rate-limit=4,
    gated=5) — the CLI contract only allows exit codes 0/1/2 (3+ reserved).
    A 400/404 means the user named something wrong; everything else is the
    environment (key, network, rate limit, server policy).
    """
    if isinstance(err, HttpError) and err.status in _USER_HTTP_STATUSES:
        return EXIT_USER_ERROR
    return EXIT_ENV_ERROR


def from_api_error(err: ApiError) -> CliError:
    """Translate one :class:`ApiError` into the equivalent :class:`CliError`."""
    return CliError(code=_exit_code(err), message=err.message, remediation=err.remediation)


def call[T](fn: Callable[[], T]) -> T:
    """Invoke ``fn``, translating any :class:`ApiError` into a :class:`CliError`."""
    try:
        return fn()
    except ApiError as err:
        raise from_api_error(err) from err


def build_client() -> SensiboClient:
    """Construct a :class:`SensiboClient`, mapping key-resolution failure to ``CliError``."""
    return call(SensiboClient)
