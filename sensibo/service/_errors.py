"""``ServiceError`` — the layering seam between this package and the CLI.

:mod:`sensibo.service` never imports :mod:`sensibo.cli` (same rule
:mod:`sensibo.store` and :mod:`sensibo.api` follow — verbs depend on the
engine, never the reverse). So a failure here raises :class:`ServiceError`
carrying the same ``{message, remediation}`` pair the CLI's error contract
needs, and ``sensibo/cli/_commands/service.py`` maps it onto a ``CliError``
with the right exit code.

Mirrors :class:`sensibo.api.ApiError`'s shape deliberately — one error idiom
across the engine packages.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ServiceError(Exception):
    """A systemd/unit-lifecycle failure, with an operator-facing remediation."""

    message: str
    remediation: str = ""

    def __post_init__(self) -> None:
        super().__init__(self.message)
