"""sensibo.health — is every sensor still talking to us? (task t2)

The retention pillar is only worth something if the operator knows the data is
still flowing. This package decides, from what a poll cycle saw, whether each
location is ``ok``, ``down``, or ``unknown`` — and what, if anything, to say
about it.

It is deliberately the **purest** layer in the repo: one function
(:func:`~sensibo.health.evaluate.evaluate`), frozen dataclasses, no clock, no
I/O, and no import of the store, the API client, the rules engine, or the CLI.
The collector owns the store and feeds this package
:class:`~sensibo.health.model.Observation` records; it persists the
:class:`~sensibo.health.model.HealthState` map, the
:class:`~sensibo.health.model.Transition` log, and hands each
:class:`~sensibo.health.model.Notification` to the transport.

:class:`~sensibo.health.model.HealthConfig` is the **single source of truth for
staleness**: the same ``down_after_seconds`` that fires an alert must drive the
STALE flag in ``room list``, the web dashboard, and the MCP locations tool, so
a dashboard can never read "fresh" for a location an alert already fired for.

Three outage classes, three distinct states — that distinction is the whole
design:

* one sensor silent while its parent pod is alive → ``down``, one alert;
* a pod down → the pod is ``down`` (one alert) and its Room Sensors are
  ``unknown_parent_down`` (no alerts);
* the collector itself failing → *every* location ``unknown`` and exactly one
  ``collector_unhealthy`` alert, never a fleet-wide false alarm.
"""

from __future__ import annotations

from .evaluate import evaluate
from .model import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_DAILY_CAP,
    DEFAULT_DOWN_AFTER_SECONDS,
    DEFAULT_RECOVERY_HOLD_CYCLES,
    ENV_COOLDOWN,
    ENV_DAILY_CAP,
    ENV_DOWN_AFTER,
    EXECUTION_LOCAL,
    NOTIFY_COLLECTOR_RECOVERED,
    NOTIFY_COLLECTOR_UNHEALTHY,
    NOTIFY_DOWN,
    NOTIFY_KINDS,
    NOTIFY_RECOVERED,
    STATUS_DOWN,
    STATUS_OK,
    STATUS_UNKNOWN,
    STATUS_UNKNOWN_PARENT_DOWN,
    STATUSES,
    CollectorOutcome,
    EvaluationResult,
    HealthConfig,
    HealthState,
    Notification,
    Observation,
    Transition,
    day_key,
    iso8601,
)

__all__ = [
    "CollectorOutcome",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_DAILY_CAP",
    "DEFAULT_DOWN_AFTER_SECONDS",
    "DEFAULT_RECOVERY_HOLD_CYCLES",
    "ENV_COOLDOWN",
    "ENV_DAILY_CAP",
    "ENV_DOWN_AFTER",
    "EXECUTION_LOCAL",
    "EvaluationResult",
    "HealthConfig",
    "HealthState",
    "NOTIFY_COLLECTOR_RECOVERED",
    "NOTIFY_COLLECTOR_UNHEALTHY",
    "NOTIFY_DOWN",
    "NOTIFY_KINDS",
    "NOTIFY_RECOVERED",
    "Notification",
    "Observation",
    "STATUSES",
    "STATUS_DOWN",
    "STATUS_OK",
    "STATUS_UNKNOWN",
    "STATUS_UNKNOWN_PARENT_DOWN",
    "Transition",
    "day_key",
    "evaluate",
    "iso8601",
]
