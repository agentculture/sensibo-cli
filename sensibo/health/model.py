"""The health data model: config, statuses, and the records the evaluator moves.

Nothing here reads a clock, a file, a socket, or a database. Every record is a
frozen dataclass and every instant is an epoch-seconds float supplied by the
caller, so :mod:`sensibo.health.evaluate` is a pure function of its inputs and
a test can replay a month of poll cycles in milliseconds.

**Layering.** This package sits *below* the store: the collector (which owns
the store) feeds it :class:`Observation` records and persists the
:class:`HealthState` map it returns. It must never import
``sensibo.store``/``sensibo.api``/``sensibo.cli`` — the guard test
``test_health_package_is_pure_stdlib`` enforces that.

**Local execution.** Health alerts run on the operator's machine and stop when
the local collect daemon stops. Every :class:`Notification` carries
:data:`EXECUTION_LOCAL` saying so, exactly like a rule does. The string is
defined here rather than imported from :mod:`sensibo.rules` so this package
stays dependency-free in both directions; the two are asserted equal by test.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: The execution marker every health notification carries — the deliberate
#: contrast with Sensibo's cloud automation, which keeps running while this
#: machine sleeps.
EXECUTION_LOCAL = "local (stops when this daemon stops)"

#: A location is reporting inside its threshold and its parent is alive.
STATUS_OK = "ok"
#: The location's own last reading is older than the threshold, or the cloud
#: reports ``connectionStatus.isAlive`` false.
STATUS_DOWN = "down"
#: The collector itself failed this cycle, so nothing can be said about any
#: location — "unknown" rather than a fleet-wide false alarm.
STATUS_UNKNOWN = "unknown"
#: A Room Sensor whose parent pod is down. Its silence is explained by the pod,
#: so it is not a sensor-down case and never notifies on its own.
STATUS_UNKNOWN_PARENT_DOWN = "unknown_parent_down"

STATUSES = (STATUS_OK, STATUS_DOWN, STATUS_UNKNOWN, STATUS_UNKNOWN_PARENT_DOWN)

#: Notification kinds. The first two are per-location; the last two are about
#: the collector process itself and carry ``location_id = None``.
NOTIFY_DOWN = "down"
NOTIFY_RECOVERED = "recovered"
NOTIFY_COLLECTOR_UNHEALTHY = "collector_unhealthy"
NOTIFY_COLLECTOR_RECOVERED = "collector_recovered"

NOTIFY_KINDS = (
    NOTIFY_DOWN,
    NOTIFY_RECOVERED,
    NOTIFY_COLLECTOR_UNHEALTHY,
    NOTIFY_COLLECTOR_RECOVERED,
)

#: Measured default, not a guess: a read-only probe of the operator's live
#: store on 2026-09-02 showed p50 = p90 = 91s and p99 = 132s inter-reading
#: intervals for the pod and both Room Sensors alike, so 900s is roughly ten
#: missed cycles — slow enough not to fire on a hiccup, far below the 24h
#: ``DEFAULT_STALE_AFTER_HOURS`` a human-glance dashboard could live with.
DEFAULT_DOWN_AFTER_SECONDS = 900.0
#: Consecutive good evaluations required before a down clears. Two cycles of
#: hysteresis; a sensor that flaps every cycle never recovers at all.
DEFAULT_RECOVERY_HOLD_CYCLES = 2
#: Minimum spacing between notifications about one location.
DEFAULT_COOLDOWN_SECONDS = 3600.0
#: Hard ceiling on notifications per location per UTC day.
DEFAULT_DAILY_CAP = 20

ENV_DOWN_AFTER = "SENSIBO_HEALTH_DOWN_AFTER"
ENV_COOLDOWN = "SENSIBO_HEALTH_COOLDOWN"
ENV_DAILY_CAP = "SENSIBO_HEALTH_DAILY_CAP"


def iso8601(timestamp: float | None) -> str:
    """Render an epoch instant as ISO-8601 UTC, or ``"never"`` for ``None``.

    Always UTC with a trailing ``Z``, never host-local: a notification is read
    later, elsewhere, possibly by an agent, and an ambiguous stamp there is a
    bug. Mirrors the format ``sensibo/collect/collector.py`` parses.
    """
    if timestamp is None:
        return "never"
    moment = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def day_key(timestamp: float) -> str:
    """The UTC calendar day an instant falls in — the daily cap's bucket."""
    return iso8601(timestamp)[:10]


def _positive_number(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {raw!r}")
    return value


def _positive_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {raw!r}")
    return value


@dataclass(frozen=True)
class HealthConfig:
    """Thresholds that drive alerting — and, from t9 on, every STALE flag.

    This is the single source of truth for staleness: the read surfaces (``room
    list``, the web dashboard, the MCP locations tool) must derive their STALE
    flag from :attr:`down_after_seconds` rather than their own 24h default, so
    the dashboard can never read "fresh" for a location an alert already fired
    for.
    """

    down_after_seconds: float = DEFAULT_DOWN_AFTER_SECONDS
    recovery_hold_cycles: int = DEFAULT_RECOVERY_HOLD_CYCLES
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    daily_cap: int = DEFAULT_DAILY_CAP

    def __post_init__(self) -> None:
        if self.down_after_seconds <= 0:
            raise ValueError("down_after_seconds must be positive")
        if self.recovery_hold_cycles < 1:
            raise ValueError("recovery_hold_cycles must be at least 1")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must not be negative")
        if self.daily_cap < 0:
            raise ValueError("daily_cap must not be negative")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "HealthConfig":
        """Build a config from the environment, defaulting anything unset.

        Reads ``SENSIBO_HEALTH_DOWN_AFTER`` (seconds), ``SENSIBO_HEALTH_COOLDOWN``
        (seconds) and ``SENSIBO_HEALTH_DAILY_CAP`` (count). An unparseable or
        negative value raises :class:`ValueError` rather than silently falling
        back to a default — a threshold the operator *thinks* they set but which
        was ignored is worse than a loud failure. The CLI layer turns this into
        a ``CliError``; this package never imports one.
        """
        source: Mapping[str, str] = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        raw_down = source.get(ENV_DOWN_AFTER)
        if raw_down is not None and raw_down != "":
            values["down_after_seconds"] = _positive_number(raw_down, ENV_DOWN_AFTER)
        raw_cooldown = source.get(ENV_COOLDOWN)
        if raw_cooldown is not None and raw_cooldown != "":
            values["cooldown_seconds"] = _positive_number(raw_cooldown, ENV_COOLDOWN)
        raw_cap = source.get(ENV_DAILY_CAP)
        if raw_cap is not None and raw_cap != "":
            values["daily_cap"] = _positive_int(raw_cap, ENV_DAILY_CAP)
        return cls(**values)

    def to_dict(self) -> dict[str, float | int]:
        return {
            "down_after_seconds": self.down_after_seconds,
            "recovery_hold_cycles": self.recovery_hold_cycles,
            "cooldown_seconds": self.cooldown_seconds,
            "daily_cap": self.daily_cap,
        }


@dataclass(frozen=True)
class Observation:
    """What one poll cycle saw about one location.

    ``last_reading_at`` is the location's **own** latest reading time
    (``measurements.time.time``), never the poll instant — a dead Room Sensor
    still rides along in every fleet snapshot carrying its old stamp, which is
    exactly the signal staleness is derived from. ``is_alive`` is
    ``connectionStatus.isAlive``, or ``None`` when the snapshot does not say.
    """

    location_id: str
    kind: str
    parent_pod_id: str | None = None
    last_reading_at: float | None = None
    is_alive: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "kind": self.kind,
            "parent_pod_id": self.parent_pod_id,
            "last_reading_at": self.last_reading_at,
            "is_alive": self.is_alive,
        }


@dataclass(frozen=True)
class HealthState:
    """One location's persisted health, carried from cycle to cycle.

    Everything the debounce needs lives here, so it survives a daemon restart
    once the collector persists it: ``ok_streak`` is the recovery hold's
    counter, and ``last_notified_at`` / ``notifications_today`` / ``day_key``
    are the cooldown and daily cap's memory.
    """

    location_id: str
    status: str
    since: float
    last_ok: float | None = None
    parent_pod_id: str | None = None
    ok_streak: int = 0
    last_notified_at: float | None = None
    notifications_today: int = 0
    day_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "status": self.status,
            "since": self.since,
            "last_ok": self.last_ok,
            "parent_pod_id": self.parent_pod_id,
            "ok_streak": self.ok_streak,
            "last_notified_at": self.last_notified_at,
            "notifications_today": self.notifications_today,
            "day_key": self.day_key,
        }


@dataclass(frozen=True)
class Transition:
    """A status change, for the append-only transitions log.

    ``from_status`` is ``None`` for a location's very first evaluation (the
    first-run seeding case).
    """

    location_id: str
    from_status: str | None
    to_status: str
    at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "at": self.at,
        }


@dataclass(frozen=True)
class Notification:
    """One message to hand to the notify transport. ``location_id`` is ``None``
    for the two collector-level kinds."""

    kind: str
    location_id: str | None
    message: str
    at: float
    execution: str = EXECUTION_LOCAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "location_id": self.location_id,
            "message": self.message,
            "at": self.at,
            "execution": self.execution,
        }


@dataclass(frozen=True)
class CollectorOutcome:
    """Whether the poll cycle that produced these observations succeeded.

    A failed cycle (cloud unreachable, 401, exhausted 429 retries) means every
    location looks silent at once; that is a collector fault, not N sensor
    faults, and the evaluator treats it as such.
    """

    ok: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "error": self.error}


@dataclass(frozen=True)
class EvaluationResult:
    """Everything one evaluation produced. ``states`` is the complete new map —
    including locations not observed this cycle, carried forward untouched."""

    states: Mapping[str, HealthState] = field(default_factory=dict)
    transitions: tuple[Transition, ...] = ()
    notifications: tuple[Notification, ...] = ()
    suppressed_count: int = 0
    collector_ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "states": {key: value.to_dict() for key, value in self.states.items()},
            "transitions": [item.to_dict() for item in self.transitions],
            "notifications": [item.to_dict() for item in self.notifications],
            "suppressed_count": self.suppressed_count,
            "collector_ok": self.collector_ok,
        }
