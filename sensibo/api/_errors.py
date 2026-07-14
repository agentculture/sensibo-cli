"""``sensibo.api``'s error shape.

Every failure raised out of :mod:`sensibo.api` is an :class:`ApiError` carrying
``code`` / ``message`` / ``remediation`` — the same shape as
:class:`sensibo.cli._errors.CliError`, so a later CLI-layer adapter can map one
onto the other. **This module does not import anything from `sensibo.cli`** —
:mod:`sensibo.api` must stay usable as a standalone library, independent of the
CLI (see ``docs/architecture.md``, "Where the Sensibo code goes").
"""

from __future__ import annotations

from dataclasses import dataclass

# Error-code categories. Independent of sensibo.cli's exit-code policy
# (0 success / 1 user error / 2 env error) — the CLI layer decides how an
# ApiError.code becomes a process exit code when it maps this onto a CliError.
ERROR_AUTH = 2
ERROR_NETWORK = 3
ERROR_RATE_LIMIT = 4
ERROR_GATED = 5


@dataclass
class ApiError(Exception):
    """Structured error raised within :mod:`sensibo.api`; carries a remediation hint."""

    code: int
    message: str
    remediation: str = ""

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }


class MissingApiKeyError(ApiError):
    """No Sensibo API key could be resolved from ``SENSIBO_API_KEY`` or ``~/.sensibo/.env``."""

    def __init__(self, message: str, remediation: str = "") -> None:
        super().__init__(code=ERROR_AUTH, message=message, remediation=remediation)


class HttpError(ApiError):
    """A non-2xx HTTP response other than the special cases below. Carries ``status``."""

    def __init__(self, message: str, status: int, remediation: str = "") -> None:
        super().__init__(code=ERROR_NETWORK, message=message, remediation=remediation)
        self.status = status


class RateLimitExceededError(ApiError):
    """HTTP 429 retries exhausted (bounded exponential backoff gave up)."""

    def __init__(self, message: str, remediation: str = "") -> None:
        super().__init__(code=ERROR_RATE_LIMIT, message=message, remediation=remediation)
        self.status = 429


class GatedHistoryWindowError(ApiError):
    """``historicalMeasurements`` returned HTTP 403 for the requested ``days`` window.

    Empirically, Sensibo gates this endpoint per-account (see
    ``docs/sensibo-api.md``, "History retention"): requesting a ``days`` value
    beyond what this account's tier allows returns 403. That is a signal to
    step the window down, not a crash — callers (the collector in a later
    task) catch this specifically to probe descending windows.
    """

    def __init__(self, pod_id: str, days: int, message: str, remediation: str = "") -> None:
        super().__init__(code=ERROR_GATED, message=message, remediation=remediation)
        self.pod_id = pod_id
        self.days = days
        self.status = 403
