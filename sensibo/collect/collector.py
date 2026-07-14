"""The collector engine.

A :class:`Collector` owns a client (anything with ``fleet_snapshot`` and
``get_historical_measurements`` — the real :class:`sensibo.api.SensiboClient`
in production, a fake in tests) and a :class:`sensibo.store.Store`. It exposes
three operations:

* :meth:`Collector.run_cycle` — one poll of the whole fleet, persisted.
* :meth:`Collector.backfill` — the first-run descending-window probe.
* :meth:`Collector.collect_once` — a cycle, plus backfill exactly on first run.

No argparse, no daemon loop, no stdout writing — those belong to the CLI verb
(``sensibo/cli/_commands/collect.py``). Everything here is deterministic and
unit-testable against a fake client and a ``tmp_path`` store.
"""

from __future__ import annotations

import datetime
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from sensibo.api import GatedHistoryWindowError
from sensibo.store import KIND_POD, KIND_ROOM_SENSOR, Store

#: Cadence defaults for the daemon. ``MIN_INTERVAL`` is Home Assistant's
#: battle-tested floor (``docs/sensibo-api.md``, "Rate limits"); the CLI
#: rejects anything below it.
DEFAULT_INTERVAL = 90
MIN_INTERVAL = 60

#: The first-run backfill probe ladder, **descending**. Empirically only
#: ``days=1`` is accessible on a non-Plus account (everything larger 403s), but
#: a paid tier may permit more — so we start high and step down.
BACKFILL_WINDOWS: tuple[int, ...] = (730, 365, 90, 30, 7, 1)

#: Store meta keys the collector owns.
META_BACKFILL_DONE = "backfill_done"
META_BACKFILL_WINDOW = "backfill_window_days"

#: A no-op logger; the CLI passes one that writes to stderr.
Logger = Callable[[str], None]


def _noop(_message: str) -> None:  # pragma: no cover - trivial default
    return None


@dataclass(frozen=True)
class CycleResult:
    """The tally from one :meth:`Collector.run_cycle`."""

    locations_seen: int
    pods: int
    room_sensors: int
    readings_written: int

    def to_dict(self) -> dict[str, int]:
        return {
            "locations_seen": self.locations_seen,
            "pods": self.pods,
            "room_sensors": self.room_sensors,
            "readings_written": self.readings_written,
        }


@dataclass(frozen=True)
class BackfillResult:
    """The outcome of a first-run :meth:`Collector.backfill`.

    ``window_days`` is the largest permitted ``historicalMeasurements`` window
    found across the fleet (``None`` if every window 403'd for every pod).
    ``per_pod`` maps each pod id to the window that worked for it.
    """

    window_days: int | None
    readings_written: int
    per_pod: dict[str, int | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "ran": True,
            "window_days": self.window_days,
            "readings_written": self.readings_written,
        }


@dataclass(frozen=True)
class CollectOnceResult:
    """One cycle, plus a backfill iff this was the store's first run."""

    cycle: CycleResult
    backfill: BackfillResult | None

    def to_summary(self) -> dict[str, object]:
        summary: dict[str, object] = dict(self.cycle.to_dict())
        summary["backfill"] = self.backfill.to_dict() if self.backfill is not None else None
        return summary


class Collector:
    """Binds a Sensibo client to a local :class:`~sensibo.store.Store`."""

    def __init__(
        self,
        client: Any,
        store: Store,
        *,
        windows: Sequence[int] = BACKFILL_WINDOWS,
        log: Logger | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._windows = tuple(windows)
        self._log = log or _noop

    # -- one cycle ---------------------------------------------------------

    def run_cycle(self, *, now: float | None = None) -> tuple[CycleResult, list]:
        """Poll the whole fleet once and persist every reported reading.

        Exactly one ``fleet_snapshot()`` call. Returns the tally and the raw
        pod records — the caller reuses those records for backfill so no second
        network round-trip is needed.
        """
        wall = time.time() if now is None else now
        snapshot = self._client.fleet_snapshot()
        pods = _extract_pods(snapshot)

        pod_count = 0
        room_count = 0
        readings = 0
        for pod in pods:
            if not isinstance(pod, Mapping):
                continue
            pod_id = pod.get("id")
            if not pod_id:
                continue
            product_model = pod.get("productModel")
            self._store.upsert_location(
                pod_id,
                kind=KIND_POD,
                product_model=product_model,
                room_name=_room_name(pod),
            )
            pod_count += 1
            readings += self._record_measurements(
                pod_id, pod.get("measurements"), product_model, wall
            )
            room_count += self._record_room_sensors(pod_id, pod.get("motionSensors"), wall)

        result = CycleResult(
            locations_seen=pod_count + room_count,
            pods=pod_count,
            room_sensors=room_count,
            readings_written=readings,
        )
        return result, pods

    def _record_room_sensors(self, parent_pod_id: str, motion_sensors: Any, wall: float) -> int:
        """Persist each Room Sensor nested under a parent pod. Returns the count."""
        if not isinstance(motion_sensors, Sequence) or isinstance(motion_sensors, (str, bytes)):
            return 0
        count = 0
        for sensor in motion_sensors:
            if not isinstance(sensor, Mapping):
                continue
            sensor_id = sensor.get("id")
            if not sensor_id:
                continue
            model = sensor.get("productModel")
            self._store.upsert_location(
                sensor_id,
                kind=KIND_ROOM_SENSOR,
                product_model=model,
                parent_pod_id=parent_pod_id,
                room_name=_room_name(sensor),
            )
            self._record_measurements(sensor_id, sensor.get("measurements"), model, wall)
            count += 1
        return count

    def _record_measurements(
        self, location_id: str, measurements: Any, product_model: str | None, wall: float
    ) -> int:
        """Store one location's measurement fields at their API reading time."""
        if not isinstance(measurements, Mapping):
            return 0
        ts = _reading_timestamp(measurements, fallback=wall)
        fields = {
            key: value
            for key, value in measurements.items()
            if key != "time" and _is_storable(value)
        }
        if not fields:
            return 0
        # product_model threads through so pm25's unit is branched on the model
        # BEFORE storage (docs/sensibo-api.md, "Trap 1: pm25 is polymorphic").
        self._store.record_readings(location_id, fields, timestamp=ts, product_model=product_model)
        return len(fields)

    # -- first-run backfill -----------------------------------------------

    def backfill(self, pods: Sequence[Any]) -> BackfillResult:
        """Probe ``historicalMeasurements`` per pod, descending, and store it.

        For each pod the windows are tried largest-first; the first that does
        not 403 (raise :class:`GatedHistoryWindowError`) is the largest
        permitted, its series is recorded, and the probe stops. The overall
        largest window found is persisted to the store meta and logged.
        """
        window: int | None = None
        total = 0
        per_pod: dict[str, int | None] = {}
        for pod in pods:
            if not isinstance(pod, Mapping):
                continue
            pod_id = pod.get("id")
            if not pod_id:
                continue
            found, written = self._probe_pod(pod_id, pod.get("productModel"))
            per_pod[pod_id] = found
            total += written
            if found is not None and (window is None or found > window):
                window = found

        self._store.set_meta(META_BACKFILL_DONE, "1")
        if window is not None:
            self._store.set_meta(META_BACKFILL_WINDOW, str(window))
            self._log(
                "collect: backfill found the largest permitted historicalMeasurements "
                f"window is days={window}"
            )
        else:
            self._log(
                "collect: backfill found no accessible historicalMeasurements window "
                "on this account"
            )
        return BackfillResult(window_days=window, readings_written=total, per_pod=per_pod)

    def _probe_pod(self, pod_id: str, product_model: str | None) -> tuple[int | None, int]:
        for days in self._windows:  # descending
            try:
                data = self._client.get_historical_measurements(pod_id, days=days)
            except GatedHistoryWindowError:
                continue  # window gated for this account — try a smaller one
            written = self._record_history(pod_id, product_model, data)
            return days, written
        return None, 0

    def _record_history(self, pod_id: str, product_model: str | None, data: Any) -> int:
        """Record every point of every series in one historical response."""
        series_map = _extract_result(data)
        if not isinstance(series_map, Mapping):
            return 0
        written = 0
        for field_name, points in series_map.items():
            if not isinstance(points, Sequence) or isinstance(points, (str, bytes)):
                continue
            for point in points:
                if not isinstance(point, Mapping) or "value" not in point:
                    continue
                ts = _parse_iso8601(point.get("time"))
                value = point.get("value")
                if ts is None or not _is_storable(value):
                    continue
                self._store.record_reading(
                    pod_id, field_name, value, timestamp=ts, product_model=product_model
                )
                written += 1
        return written

    # -- orchestration -----------------------------------------------------

    def collect_once(self, *, now: float | None = None) -> CollectOnceResult:
        """One cycle, plus a first-run backfill iff the store hasn't done one.

        The "first run" is decided by the ``backfill_done`` meta flag, so the
        expensive descending probe happens exactly once per store, not every
        cycle — even across daemon restarts.
        """
        cycle, pods = self.run_cycle(now=now)
        backfill: BackfillResult | None = None
        if self._store.get_meta(META_BACKFILL_DONE) is None:
            backfill = self.backfill(pods)
        return CollectOnceResult(cycle=cycle, backfill=backfill)


# --- pure helpers ---------------------------------------------------------


def _extract_pods(snapshot: Any) -> list:
    """Pull the pod list out of a ``fleet_snapshot`` envelope (or a bare list)."""
    if isinstance(snapshot, Mapping):
        result = snapshot.get("result")
        if isinstance(result, list):
            return result
    if isinstance(snapshot, list):
        return snapshot
    return []


def _extract_result(data: Any) -> Any:
    """Unwrap the ``{"result": ...}`` envelope Sensibo wraps responses in."""
    if isinstance(data, Mapping) and "result" in data:
        return data["result"]
    return data


def _room_name(obj: Mapping) -> str | None:
    room = obj.get("room")
    if isinstance(room, Mapping):
        name = room.get("name")
        if isinstance(name, str):
            return name
    return None


def _is_storable(value: Any) -> bool:
    """A scalar the store can hold — numbers, bools, strings; never a nested
    object or list (which would stringify into garbage)."""
    return isinstance(value, (int, float, str, bool))


def _reading_timestamp(measurements: Mapping, *, fallback: float) -> float:
    """The instant a measurement was taken, from the API — not the wall clock.

    Sensibo nests the reading time as ``measurements["time"]["time"]`` (an
    ISO-8601 string). Using it is what makes re-collection idempotent. Only if
    it is missing or unparseable do we fall back to ``fallback`` (usually now).
    """
    marker = measurements.get("time")
    iso: Any = None
    if isinstance(marker, Mapping):
        iso = marker.get("time")
    elif isinstance(marker, str):
        iso = marker
    parsed = _parse_iso8601(iso)
    return parsed if parsed is not None else fallback


def _parse_iso8601(value: Any) -> float | None:
    """Parse an ISO-8601 timestamp (with a trailing ``Z``) to epoch seconds."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()
