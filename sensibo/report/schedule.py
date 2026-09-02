"""Report scheduling: when a daily/weekly report is due (task t7).

Pure and clock-injectable — no store, no notify, no filesystem, no import of
:mod:`sensibo.cli`. :class:`ReportSchedule` is a frozen, validated dataclass;
:func:`due_reports` is the one function that decides "is this due right now?"
from the schedule, the current instant, and each kind's last-sent instant.

**Catch-up semantics.** A report is due when the *most recent* scheduled
instant at or before ``now`` is later than the last-sent instant — not "how
many instants were missed". A daemon that was down across several missed
7am's therefore gets at most one catch-up report per kind when it comes back,
never a backlog of one per missed day.

Times are host-local (``tz`` defaults to the host's local zone via
``datetime.now().astimezone().tzinfo``, injectable for deterministic tests) —
an operator picks "07:00" meaning their own wall clock, not UTC.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Mapping
from dataclasses import dataclass

#: Environment variables read by :meth:`ReportSchedule.from_env`.
DAILY_AT_VAR = "SENSIBO_REPORT_DAILY_AT"
WEEKLY_AT_VAR = "SENSIBO_REPORT_WEEKLY_AT"
WEEKLY_DAY_VAR = "SENSIBO_REPORT_WEEKLY_DAY"

#: Report kinds returned by :func:`due_reports`.
DAILY = "daily"
WEEKLY = "weekly"

#: Store meta keys the caller reads/writes the last-sent instant under
#: (``repr(float)`` of the epoch instant a report was last successfully
#: rendered and written for).
META_LAST_DAILY = "last_daily_report_at"
META_LAST_WEEKLY = "last_weekly_report_at"


def _parse_hhmm(value: str) -> tuple[int, int]:
    """Parse an ``HH:MM`` string; raise :class:`ValueError` on anything else."""
    try:
        moment = datetime.datetime.strptime(value, "%H:%M")
    except (ValueError, TypeError) as err:
        raise ValueError(f"expected time as HH:MM, got {value!r}") from err
    return moment.hour, moment.minute


@dataclass(frozen=True)
class ReportSchedule:
    """When daily and weekly reports are due, in host-local wall-clock time.

    ``weekly_day`` follows :meth:`datetime.date.weekday`: ``0`` is Monday,
    ``6`` is Sunday (matching the default, Monday).
    """

    daily_at: str = "07:00"
    weekly_at: str = "07:00"
    weekly_day: int = 0

    def __post_init__(self) -> None:
        _parse_hhmm(self.daily_at)
        _parse_hhmm(self.weekly_at)
        if not 0 <= self.weekly_day <= 6:
            raise ValueError(f"weekly_day must be 0-6 (0=Monday), got {self.weekly_day!r}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ReportSchedule":
        """Build a schedule from ``SENSIBO_REPORT_*`` env vars, defaults otherwise.

        ``env`` is injectable so tests never read the real process
        environment. Production code calls this with no arguments, which
        reads ``os.environ``. An unset or empty value takes the default;
        an out-of-range or malformed value raises :class:`ValueError` (the
        CLI boundary maps that onto ``CliError`` code 1).
        """
        environ = env if env is not None else os.environ
        kwargs: dict[str, object] = {}

        daily_at = environ.get(DAILY_AT_VAR)
        if daily_at:
            kwargs["daily_at"] = daily_at

        weekly_at = environ.get(WEEKLY_AT_VAR)
        if weekly_at:
            kwargs["weekly_at"] = weekly_at

        weekly_day = environ.get(WEEKLY_DAY_VAR)
        if weekly_day:
            try:
                kwargs["weekly_day"] = int(weekly_day)
            except ValueError as err:
                raise ValueError(
                    f"{WEEKLY_DAY_VAR} must be an integer 0-6 (0=Monday), got {weekly_day!r}"
                ) from err

        return cls(**kwargs)  # type: ignore[arg-type]


def _resolve_tz(tz: datetime.tzinfo | None) -> datetime.tzinfo | None:
    if tz is not None:
        return tz
    return datetime.datetime.now().astimezone().tzinfo


def _most_recent_daily_instant(at: str, now: float, tz: datetime.tzinfo | None) -> float:
    """The most recent local ``HH:MM`` instant at or before ``now``, as epoch."""
    hour, minute = _parse_hhmm(at)
    now_local = datetime.datetime.fromtimestamp(now, tz=tz)
    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > now_local:
        candidate -= datetime.timedelta(days=1)
    return candidate.timestamp()


def _most_recent_weekly_instant(
    at: str, weekly_day: int, now: float, tz: datetime.tzinfo | None
) -> float:
    """The most recent local ``weekly_day``/``HH:MM`` instant at or before ``now``."""
    hour, minute = _parse_hhmm(at)
    now_local = datetime.datetime.fromtimestamp(now, tz=tz)
    days_since_scheduled = (now_local.weekday() - weekly_day) % 7
    candidate_day = now_local - datetime.timedelta(days=days_since_scheduled)
    candidate = candidate_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > now_local:
        candidate -= datetime.timedelta(days=7)
    return candidate.timestamp()


def due_reports(
    schedule: ReportSchedule,
    now: float,
    last_daily_at: float | None,
    last_weekly_at: float | None,
    tz: datetime.tzinfo | None = None,
) -> list[str]:
    """Which of ``"daily"``/``"weekly"`` are due right now.

    A kind is due when the most recent scheduled instant at or before ``now``
    is later than its last-sent instant (``None`` counts as "never sent", so
    it is always due). Order in the returned list is always daily then
    weekly, when both are due.
    """
    resolved_tz = _resolve_tz(tz)
    due: list[str] = []

    daily_instant = _most_recent_daily_instant(schedule.daily_at, now, resolved_tz)
    if last_daily_at is None or daily_instant > last_daily_at:
        due.append(DAILY)

    weekly_instant = _most_recent_weekly_instant(
        schedule.weekly_at, schedule.weekly_day, now, resolved_tz
    )
    if last_weekly_at is None or weekly_instant > last_weekly_at:
        due.append(WEEKLY)

    return due
