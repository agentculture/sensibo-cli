"""``sensibo.collect`` — the collector: sensibo-cli's retention thesis, running.

This is the layer that stitches :mod:`sensibo.api` (the cloud client) to
:mod:`sensibo.store` (the local time-series db). It is the only package
allowed to import *both* — the api and store packages stay independent of each
other and of the CLI, so this orchestration layer sits above them and the CLI
verb (``sensibo/cli/_commands/collect.py``) sits above this. It imports nothing
from :mod:`sensibo.cli`, so it stays testable and reusable without argparse.

What the collector guarantees, all load-bearing (see ``docs/sensibo-api.md``):

* **One cycle is exactly one** ``fleet_snapshot()`` **call** — never a
  per-device loop, which would blow the rate limit.
* **pm25 is branched on ``productModel`` before storage** — threaded through
  :meth:`sensibo.store.Store.record_readings` so a Pure pod's AQI enum and an
  Elements pod's µg/m³ never share a unit tag.
* **Timestamps come from the API's own reading times, not the wall clock**, so
  re-collecting an overlapping window upserts rather than duplicating.
* **A Room Sensor is not a pod** — it is persisted under its stable ``ms_*``
  id with the parent pod recorded, from the parent's ``motionSensors[]``.
* **Every cycle judges sensor health** — one :class:`~sensibo.health.Observation`
  per location (its own reading time plus ``connectionStatus.isAlive``) through
  the pure evaluator, with the whole resulting state map persisted so "exactly
  one alert per outage" survives a daemon restart, and undelivered alerts
  queued for the next cycle rather than lost.
* **A failed poll is a collector fault, not N sensor faults** — the cycle is
  recorded as failed, every location goes ``unknown``, one collector alert
  fires on the edge, and the daemon keeps its cadence.
* **First-run backfill probes descending windows** and treats an HTTP 403 /
  :class:`~sensibo.api.GatedHistoryWindowError` as "window gated, try smaller",
  not an error — landing on the largest permitted ``days`` and persisting that
  finding into the store's meta table.
"""

from __future__ import annotations

from sensibo.collect.collector import (
    BACKFILL_WINDOWS,
    DEFAULT_INTERVAL,
    MAX_OWED,
    MAX_OWED_ATTEMPTS,
    META_BACKFILL_DONE,
    META_BACKFILL_WINDOW,
    META_COLLECT_INTERVAL,
    META_COLLECTOR_OK,
    META_HEALTH_EXTRA,
    META_HEALTH_OWED,
    META_LAST_CYCLE_AT,
    META_LAST_CYCLE_OUTCOME,
    MIN_INTERVAL,
    BackfillResult,
    CollectOnceResult,
    Collector,
    CycleResult,
    default_notifier,
)

__all__ = [
    "Collector",
    "CycleResult",
    "BackfillResult",
    "CollectOnceResult",
    "BACKFILL_WINDOWS",
    "DEFAULT_INTERVAL",
    "MAX_OWED",
    "MAX_OWED_ATTEMPTS",
    "MIN_INTERVAL",
    "META_BACKFILL_DONE",
    "META_BACKFILL_WINDOW",
    "META_COLLECT_INTERVAL",
    "META_COLLECTOR_OK",
    "META_HEALTH_EXTRA",
    "META_HEALTH_OWED",
    "META_LAST_CYCLE_AT",
    "META_LAST_CYCLE_OUTCOME",
    "default_notifier",
]
