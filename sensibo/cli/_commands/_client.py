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

from typing import Callable, TypeVar

from sensibo.api import ApiError, SensiboClient
from sensibo.cli._errors import CliError

_T = TypeVar("_T")


def call(fn: Callable[[], _T]) -> _T:
    """Invoke ``fn``, translating any :class:`ApiError` into a :class:`CliError`."""
    try:
        return fn()
    except ApiError as err:
        raise CliError(code=err.code, message=err.message, remediation=err.remediation) from err


def build_client() -> SensiboClient:
    """Construct a :class:`SensiboClient`, mapping key-resolution failure to ``CliError``."""
    return call(SensiboClient)
