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

Times are host-local. With ``tz`` omitted (``None``), candidates are built as
**naive** local datetimes and converted back to epoch via
:meth:`datetime.datetime.astimezone` — naive means "system local", so each
candidate's own date carries the correct DST offset for *that* date, never a
single fixed offset reused across a DST transition (Qodo review Q16). ``tz``
stays injectable for deterministic tests (e.g. a fixed
:class:`datetime.timezone` or a dynamic :class:`zoneinfo.ZoneInfo`) — an
operator picks "07:00" meaning their own wall clock, not UTC.
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


def _local_now(now: float, tz: datetime.tzinfo | None) -> datetime.datetime:
    """``now`` as a local datetime: tz-aware when ``tz`` is given, else naive.

    A **naive** datetime here means "the host's system local time" — exactly
    what :meth:`datetime.datetime.timestamp` assumes for a naive instance
    (it defers to the platform's ``mktime``, which honors DST for whatever
    date the naive value actually falls on).
    """
    if tz is not None:
        return datetime.datetime.fromtimestamp(now, tz=tz)
    return datetime.datetime.fromtimestamp(now)  # naive, system-local


def _to_epoch(candidate: datetime.datetime, tz: datetime.tzinfo | None) -> float:
    """Convert a (possibly naive) local candidate back to an epoch instant.

    With ``tz`` given, ``candidate`` is already aware of that (dynamic, e.g.
    :mod:`zoneinfo`) zone and ``.timestamp()`` alone is correct. With ``tz``
    ``None``, ``candidate`` is naive local; ``.astimezone()`` (no argument)
    attaches the system zone's *correct offset for that date* — this is what
    keeps a DST transition from silently reusing an earlier candidate's fixed
    offset (Qodo review Q16), unlike computing one fixed offset from "now"
    and reusing it for every candidate date.
    """
    if tz is not None:
        return candidate.timestamp()
    return candidate.astimezone().timestamp()


def _most_recent_daily_instant(at: str, now: float, tz: datetime.tzinfo | None) -> float:
    """The most recent local ``HH:MM`` instant at or before ``now``, as epoch."""
    hour, minute = _parse_hhmm(at)
    now_local = _local_now(now, tz)
    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > now_local:
        candidate -= datetime.timedelta(days=1)
    return _to_epoch(candidate, tz)


def _most_recent_weekly_instant(
    at: str, weekly_day: int, now: float, tz: datetime.tzinfo | None
) -> float:
    """The most recent local ``weekly_day``/``HH:MM`` instant at or before ``now``."""
    hour, minute = _parse_hhmm(at)
    now_local = _local_now(now, tz)
    days_since_scheduled = (now_local.weekday() - weekly_day) % 7
    candidate_day = now_local - datetime.timedelta(days=days_since_scheduled)
    candidate = candidate_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > now_local:
        candidate -= datetime.timedelta(days=7)
    return _to_epoch(candidate, tz)


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
    due: list[str] = []

    daily_instant = _most_recent_daily_instant(schedule.daily_at, now, tz)
    if last_daily_at is None or daily_instant > last_daily_at:
        due.append(DAILY)

    weekly_instant = _most_recent_weekly_instant(schedule.weekly_at, schedule.weekly_day, now, tz)
    if last_weekly_at is None or weekly_instant > last_weekly_at:
        due.append(WEEKLY)

    return due
