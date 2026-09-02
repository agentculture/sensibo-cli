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
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from sensibo.api import ApiError, GatedHistoryWindowError, scrub_text
from sensibo.health import (
    STATUS_DOWN,
    STATUS_OK,
    CollectorOutcome,
    EvaluationResult,
    HealthConfig,
    HealthState,
    Notification,
    Observation,
    evaluate,
    iso8601,
)
from sensibo.notify import Payload, resolve_notify_config, send
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
#: When the last cycle ran (epoch seconds, ``repr``-formatted).
META_LAST_CYCLE_AT = "last_cycle_at"
#: ``"ok"`` or ``"failed: <redacted reason>"`` — the last cycle's verdict.
META_LAST_CYCLE_OUTCOME = "last_cycle_outcome"
#: ``"1"``/``"0"``: the last cycle's collector verdict, fed back to
#: :func:`sensibo.health.evaluate` as ``collector_previous_ok`` so the
#: collector-level notifications stay edge-triggered across a restart.
META_COLLECTOR_OK = "collector_ok"
#: JSON blob of the :class:`~sensibo.health.HealthState` fields the store's
#: ``health`` table has no column for (``ok_streak``, ``last_notified_at``,
#: ``notifications_today``, ``day_key``), keyed by location id. Without these
#: a restart would forget the cooldown and re-announce an open outage.
META_HEALTH_EXTRA = "health_state_extra"
#: JSON list of notifications whose delivery failed and are still owed. A
#: transition with ``notified_at`` NULL is owed *only* if it had a
#: notification to begin with (most transitions — seeding, parent-sheltered
#: children — never notify), so the debt is queued explicitly rather than
#: inferred from every un-stamped row.
META_HEALTH_OWED = "health_owed"

#: The poll interval the running daemon persists each cycle, ``repr``-formatted.
#: ``sensibo doctor``'s ``collector_heartbeat`` check reads it so a deliberately
#: slow cadence is not mistaken for a dead collector.
META_COLLECT_INTERVAL = "collect_interval"

#: Hard ceiling on the owed queue, so a long transport outage cannot grow the
#: meta blob without bound; the oldest entries are dropped first.
MAX_OWED = 50

#: How many cycles an undelivered notification is retried before it is dropped.
#: There is deliberately **no exponential backoff**: the daemon's cycle cadence
#: is already the retry spacing (``MIN_INTERVAL`` is 60s, so 20 attempts is
#: ~30 minutes at the 90s default), and adding a second timer on top would only
#: make "when does this alert give up?" harder to reason about. The drop is
#: recorded as a notification row with a ``dropped after N attempts`` outcome,
#: so a debt that expired is visible rather than silent.
MAX_OWED_ATTEMPTS = 20

#: A no-op logger; the CLI passes one that writes to stderr.
Logger = Callable[[str], None]

#: A notify transport: takes a payload, returns one outcome per transport.
#: Injectable so tests never resolve the operator's real notify config. It is
#: called with a second positional argument — a list of transport names — only
#: when a *partial* redelivery is being retried, so a one-argument callable
#: remains a valid notifier for everything else.
Notifier = Callable[..., Sequence[Any]]


def _noop(_message: str) -> None:  # pragma: no cover - trivial default
    return None


def default_notifier(payload: Payload, only: Sequence[str] | None = None) -> Sequence[Any]:
    """Deliver through :mod:`sensibo.notify` with the operator's resolved config.

    Resolved per call rather than cached, so an operator can add a webhook to
    ``~/.sensibo/.env`` without restarting the daemon. :func:`sensibo.notify.send`
    never raises — a flaky webhook must not take down the cycle that detected
    the outage.

    ``only`` restricts the retry to the transports that still owe delivery, so
    a webhook that already accepted an alert is not spammed while the operator
    script is being retried.
    """
    return send(payload, resolve_notify_config(), only)


@dataclass(frozen=True)
class CycleResult:
    """The tally from one :meth:`Collector.run_cycle`.

    The ``health_*`` counts are over the *whole* persisted health map, not just
    the locations this cycle saw — a location the snapshot omitted still has an
    open outage. ``health_unknown`` folds ``unknown`` and
    ``unknown_parent_down`` together: both mean "nothing can be said", which is
    what a summary line needs to convey.
    """

    locations_seen: int
    pods: int
    room_sensors: int
    readings_written: int
    health_ok: int = 0
    health_down: int = 0
    health_unknown: int = 0
    notifications_sent: int = 0
    notifications_suppressed: int = 0

    def health_dict(self) -> dict[str, int]:
        return {
            "ok": self.health_ok,
            "down": self.health_down,
            "unknown": self.health_unknown,
            "notifications_sent": self.notifications_sent,
            "notifications_suppressed": self.notifications_suppressed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "locations_seen": self.locations_seen,
            "pods": self.pods,
            "room_sensors": self.room_sensors,
            "readings_written": self.readings_written,
            "health": self.health_dict(),
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
        notifier: Notifier | None = None,
        health_config: HealthConfig | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._windows = tuple(windows)
        self._log = log or _noop
        self._notifier: Notifier = notifier or default_notifier
        self._health_config = health_config or HealthConfig()

    # -- one cycle ---------------------------------------------------------

    def run_cycle(self, *, now: float | None = None) -> tuple[CycleResult, list]:
        """Poll the whole fleet once, persist every reading, and judge health.

        Exactly one ``fleet_snapshot()`` call. Returns the tally and the raw
        pod records — the caller reuses those records for backfill so no second
        network round-trip is needed.

        A failed poll is still a *cycle*: the failure is recorded, the health
        engine is run with a failed :class:`~sensibo.health.CollectorOutcome`
        (so every location goes ``unknown`` rather than a fleet-wide false
        "down" alarm), and only then is the :class:`~sensibo.api.ApiError`
        re-raised for the caller to decide about — ``--once`` exits 2, the
        daemon logs and keeps its cadence.
        """
        wall = time.time() if now is None else now
        try:
            snapshot = self._client.fleet_snapshot()
        except ApiError as err:
            # Scrub BEFORE the evaluator sees it: this string is quoted verbatim
            # into the outbound collector_unhealthy notification, and a Sensibo
            # URL carries the API key as a query parameter.
            self._finish_cycle(wall, ok=False, error=scrub_text(str(err)), observations=())
            raise
        pods = _extract_pods(snapshot)

        pod_count = 0
        room_count = 0
        readings = 0
        observations: list[Observation] = []
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
                seen_at=_seen_at(pod.get("measurements"), wall),
            )
            pod_count += 1
            observations.append(_observe(pod, pod_id, KIND_POD, None))
            readings += self._record_measurements(
                pod_id, pod.get("measurements"), product_model, wall
            )
            sensors_seen, sensor_readings = self._record_room_sensors(
                pod_id, pod.get("motionSensors"), wall, observations
            )
            room_count += sensors_seen
            readings += sensor_readings

        evaluation, sent = self._finish_cycle(wall, ok=True, error=None, observations=observations)
        ok_count, down_count, unknown_count = _health_counts(evaluation.states)
        result = CycleResult(
            locations_seen=pod_count + room_count,
            pods=pod_count,
            room_sensors=room_count,
            readings_written=readings,
            health_ok=ok_count,
            health_down=down_count,
            health_unknown=unknown_count,
            notifications_sent=sent,
            notifications_suppressed=evaluation.suppressed_count,
        )
        return result, pods

    def _record_room_sensors(
        self,
        parent_pod_id: str,
        motion_sensors: Any,
        wall: float,
        observations: list[Observation] | None = None,
    ) -> tuple[int, int]:
        """Persist each Room Sensor nested under a parent pod.

        Returns ``(sensor_count, readings_written)`` — Qodo 3581287844 flagged
        that the readings count used to be discarded here, so a Room Sensor's
        fields landed in the store but never showed up in
        ``CycleResult.readings_written``.
        """
        if not isinstance(motion_sensors, Sequence) or isinstance(motion_sensors, (str, bytes)):
            return 0, 0
        count = 0
        readings = 0
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
                seen_at=_seen_at(sensor.get("measurements"), wall),
            )
            if observations is not None:
                observations.append(_observe(sensor, sensor_id, KIND_ROOM_SENSOR, parent_pod_id))
            readings += self._record_measurements(
                sensor_id, sensor.get("measurements"), model, wall
            )
            count += 1
        return count, readings

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

    # -- health: evaluate, persist, alert ----------------------------------

    def load_health_states(self) -> dict[str, HealthState]:
        """Rebuild the health map from the store — the restart-safe read.

        The ``health`` table holds the columns an operator queries (status,
        since, last_ok, parent); the debounce fields live alongside it in the
        :data:`META_HEALTH_EXTRA` blob. Loading from the store rather than from
        an in-memory map is what makes "exactly one alert per outage" survive a
        daemon restart.
        """
        extras = self._load_json(META_HEALTH_EXTRA, {})
        states: dict[str, HealthState] = {}
        for record in self._store.list_health():
            extra = extras.get(record.location_id) or {}
            states[record.location_id] = HealthState(
                location_id=record.location_id,
                status=record.status,
                since=record.since,
                last_ok=record.last_ok,
                parent_pod_id=record.parent_pod_id,
                ok_streak=int(extra.get("ok_streak", 0) or 0),
                last_notified_at=extra.get("last_notified_at"),
                notifications_today=int(extra.get("notifications_today", 0) or 0),
                day_key=extra.get("day_key"),
                announced_down_since=extra.get("announced_down_since"),
            )
        return states

    def _finish_cycle(
        self,
        wall: float,
        *,
        ok: bool,
        error: str | None,
        observations: Sequence[Observation],
    ) -> tuple[EvaluationResult, int]:
        """Run the health engine for this cycle and persist everything it says.

        The order here is the crash-safety contract. Retry the standing debt,
        judge the cycle, then commit — in **one** store transaction — the new
        health rows, this cycle's transitions, the cycle meta, and the
        owed-notification queue holding *every* alert this cycle wants to send.
        Only after that commit is a transport touched, and a delivery that
        succeeds clears its own debt.

        Committing the health state first (as this used to) loses alerts: the
        state says "down", so the next cycle sees no edge and never re-announces
        it, while the transition row and its debt died with the process. Now a
        process exit anywhere after the commit leaves the alert owed, and the
        next cycle's :meth:`_retry_owed` delivers it.
        """
        carried = self._retry_owed(wall)
        previous = self.load_health_states()
        evaluation = evaluate(
            previous,
            observations,
            CollectorOutcome(ok=ok, error=error),
            wall,
            self._health_config,
            self._collector_previous_ok(),
        )
        reason = scrub_text(error or "the fleet snapshot failed")
        meta = {
            META_HEALTH_EXTRA: json.dumps(_health_extras(evaluation.states), sort_keys=True),
            META_LAST_CYCLE_AT: repr(wall),
            META_LAST_CYCLE_OUTCOME: "ok" if ok else f"failed: {reason}",
            META_COLLECTOR_OK: "1" if evaluation.collector_ok else "0",
        }

        pending: list[dict[str, Any]] = []

        def _owed_meta(transition_ids: list[int]) -> dict[str, str]:
            by_location = {
                transition.location_id: transition_id
                for transition, transition_id in zip(evaluation.transitions, transition_ids)
            }
            for note in evaluation.notifications:
                payload = _payload_for(note, evaluation.states.get(note.location_id or ""))
                pending.append(
                    {
                        "kind": note.kind,
                        "location_id": note.location_id,
                        "transition_id": (
                            by_location.get(note.location_id)
                            if note.location_id is not None
                            else None
                        ),
                        "payload": _payload_dict(payload),
                        "attempts": 0,
                        "transports": None,
                    }
                )
            return {META_HEALTH_OWED: _dump_owed(carried + pending)}

        self._store.persist_health_cycle(
            health_rows=[
                (state.location_id, state.status, state.since, state.last_ok, state.parent_pod_id)
                for state in evaluation.states.values()
            ],
            transitions=[
                (t.location_id, t.from_status, t.to_status, t.at) for t in evaluation.transitions
            ],
            meta=meta,
            meta_from_transitions=_owed_meta,
        )

        sent = self._dispatch(pending, carried, wall)
        return evaluation, sent

    def _collector_previous_ok(self) -> bool | None:
        raw = self._store.get_meta(META_COLLECTOR_OK)
        if raw is None:
            return None
        return raw == "1"

    def _dispatch(
        self, pending: Sequence[dict[str, Any]], carried: Sequence[dict[str, Any]], wall: float
    ) -> int:
        """Try to deliver this cycle's already-persisted notifications.

        Every entry in ``pending`` is on disk as owed before this runs, so the
        only thing left to persist is the *reduction* of that debt. A transport
        that accepted the message stamps ``notified_at``; a transport that did
        not keeps its own debt (``transports``), so a webhook success does not
        cancel a script failure.
        """
        sent = 0
        surviving: list[dict[str, Any]] = []
        for entry in pending:
            payload = _payload_from_dict(entry["payload"])
            if payload is None:  # pragma: no cover - we just built this dict
                continue
            succeeded, failed = self._deliver(payload, entry["kind"], entry["location_id"], wall)
            if succeeded:
                sent += 1
                if entry["transition_id"] is not None:
                    self._store.mark_transition_notified(int(entry["transition_id"]), wall)
            if failed is None or failed:
                entry["attempts"] = 1
                entry["transports"] = failed
                surviving.append(entry)
        self._save_owed(list(carried) + surviving)
        return sent

    def _retry_owed(self, wall: float) -> list[dict[str, Any]]:
        """Retry the standing notification debt once; return what is still owed.

        One attempt per cycle, with no backoff schedule of its own: the daemon's
        cadence (``MIN_INTERVAL`` is 60s) already spaces the retries, so a second
        timer would only obscure when an alert gives up. After
        :data:`MAX_OWED_ATTEMPTS` cycles the entry is dropped — logged once
        (redacted) and recorded as a notification row — rather than retried
        forever.
        """
        remaining: list[dict[str, Any]] = []
        for raw_entry in self._load_json(META_HEALTH_OWED, []):
            if not isinstance(raw_entry, Mapping):
                continue  # unreadable entry: drop it rather than retry forever
            still_owed = self._retry_one(dict(raw_entry), wall)
            if still_owed is not None:
                remaining.append(still_owed)
        return remaining

    def _retry_one(self, entry: dict[str, Any], wall: float) -> dict[str, Any] | None:
        """One delivery attempt for one owed entry; the entry back if still owed."""
        payload = _payload_from_dict(entry.get("payload"))
        if payload is None:
            return None  # unreadable entry: drop it rather than retry forever
        kind = str(entry.get("kind", ""))
        location_id = entry.get("location_id")
        attempts = _int_or_zero(entry.get("attempts")) + 1
        succeeded, failed = self._deliver(
            payload, kind, location_id, wall, only=entry.get("transports") or None
        )
        transition_id = entry.get("transition_id")
        if succeeded and transition_id is not None:
            self._store.mark_transition_notified(int(transition_id), wall)
        if failed is not None and not failed:
            return None  # every transport this entry still owed has accepted it
        if attempts >= MAX_OWED_ATTEMPTS:
            self._drop_owed(kind, location_id, failed, attempts, wall)
            return None
        entry["attempts"] = attempts
        entry["transports"] = failed
        return entry

    def _drop_owed(
        self,
        kind: str,
        location_id: Any,
        failed: list[str] | None,
        attempts: int,
        wall: float,
    ) -> None:
        """Give up on one owed notification: log it once and record the give-up."""
        transports = ",".join(failed) if failed else "unknown"
        self._log(
            scrub_text(
                f"collect: giving up on an undelivered {kind} notification for "
                f"{location_id or 'collector'} via {transports} after {attempts} attempts"
            )
        )
        self._store.record_notification(
            kind=kind,
            location_id=location_id,
            sent_at=wall,
            transport=transports,
            outcome=f"dropped after {attempts} attempts",
        )

    def _deliver(
        self,
        payload: Payload,
        kind: str,
        location_id: str | None,
        wall: float,
        only: Sequence[str] | None = None,
    ) -> tuple[bool, list[str] | None]:
        """Hand one payload to the transport; record every attempt's outcome.

        Returns ``(any transport accepted it, the transports that did not)``.
        The second element is ``None`` for "we cannot tell which legs failed —
        retry them all", ``[]`` for "nothing is still owed", and otherwise the
        named transports that still owe delivery.
        """
        try:
            outcomes = self._notifier(payload) if only is None else self._notifier(payload, only)
        except Exception as exc:  # pragma: no cover - a transport must not raise
            self._store.record_notification(
                kind=kind,
                location_id=location_id,
                sent_at=wall,
                transport="unknown",
                outcome=f"failed: {scrub_text(str(exc))}",
            )
            return False, (list(only) if only else None)
        succeeded = False
        failed: list[str] = []
        for outcome in outcomes or ():
            ok = bool(getattr(outcome, "ok", False))
            transport = str(getattr(outcome, "transport", "unknown"))
            if ok:
                succeeded = True
            else:
                failed.append(transport)
            self._store.record_notification(
                kind=kind,
                location_id=location_id,
                sent_at=wall,
                transport=transport,
                outcome=("delivered" if ok else f"failed: {getattr(outcome, 'detail', 'unknown')}"),
            )
        return succeeded, failed

    def _load_json(self, key: str, default: Any) -> Any:
        raw = self._store.get_meta(key)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except ValueError:  # pragma: no cover - a corrupt blob is not fatal
            return default

    def _save_owed(self, owed: Sequence[Any]) -> None:
        self._store.set_meta(META_HEALTH_OWED, _dump_owed(owed))

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


def _observe(obj: Mapping, location_id: str, kind: str, parent_pod_id: str | None) -> Observation:
    """Build this cycle's health observation for one location.

    ``last_reading_at`` is the location's **own** ``measurements.time.time``,
    never the poll instant, and ``None`` when it reports no time at all — a
    location in the fleet with no reading behind it is exactly the
    battery-dead case, and stamping "now" would hide it forever.
    """
    return Observation(
        location_id=location_id,
        kind=kind,
        parent_pod_id=parent_pod_id,
        last_reading_at=_own_reading_at(obj.get("measurements")),
        is_alive=_is_alive(obj.get("connectionStatus")),
    )


def _own_reading_at(measurements: Any) -> float | None:
    if not isinstance(measurements, Mapping):
        return None
    marker = measurements.get("time")
    iso: Any = marker.get("time") if isinstance(marker, Mapping) else marker
    return _parse_iso8601(iso)


def _is_alive(connection_status: Any) -> bool | None:
    """``connectionStatus.isAlive``, or ``None`` when the snapshot doesn't say."""
    if not isinstance(connection_status, Mapping):
        return None
    value = connection_status.get("isAlive")
    return bool(value) if isinstance(value, bool) else None


def _health_counts(states: Mapping[str, HealthState]) -> tuple[int, int, int]:
    """``(ok, down, unknown)`` over the whole persisted health map."""
    ok = down = unknown = 0
    for state in states.values():
        if state.status == STATUS_OK:
            ok += 1
        elif state.status == STATUS_DOWN:
            down += 1
        else:
            unknown += 1
    return ok, down, unknown


def _payload_for(note: Notification, state: HealthState | None) -> Payload:
    """Render one notification as the JSON document a transport receives."""
    if state is not None:
        status = state.status
        since = iso8601(state.since)
        last_ok = iso8601(state.last_ok)
    else:
        status = "unknown"
        since = iso8601(note.at)
        last_ok = iso8601(None)
    return Payload(
        kind=note.kind,
        location=note.location_id or "collector",
        status=status,
        since=since,
        last_ok=last_ok,
        message=note.message,
        execution=note.execution,
    )


def _health_extras(states: Mapping[str, HealthState]) -> dict[str, dict[str, Any]]:
    """The HealthState fields the ``health`` table has no column for."""
    return {
        location_id: {
            "ok_streak": state.ok_streak,
            "last_notified_at": state.last_notified_at,
            "notifications_today": state.notifications_today,
            "day_key": state.day_key,
            "announced_down_since": state.announced_down_since,
        }
        for location_id, state in states.items()
    }


def _dump_owed(owed: Sequence[Any]) -> str:
    """Serialise the owed queue, oldest entries dropped past :data:`MAX_OWED`."""
    return json.dumps(list(owed)[-MAX_OWED:], sort_keys=True)


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):  # pragma: no cover - a corrupt entry
        return 0


def _payload_dict(payload: Payload) -> dict[str, str]:
    return {
        "kind": payload.kind,
        "location": payload.location,
        "status": payload.status,
        "since": payload.since,
        "last_ok": payload.last_ok,
        "message": payload.message,
        "execution": payload.execution,
    }


def _payload_from_dict(data: Any) -> Payload | None:
    if not isinstance(data, Mapping):
        return None
    try:
        return Payload(**{key: str(value) for key, value in data.items()})
    except TypeError:  # pragma: no cover - an unreadable queued entry
        return None


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


def _seen_at(measurements: Any, wall: float) -> float:
    """A location's ``last_seen``: its own latest reading time, never the poll.

    A Room Sensor that died months ago still rides along in every fleet
    snapshot carrying its *old* ``time.time`` stamp — stamping the poll
    instant would make a dead sensor look alive forever (caught against the
    operator's real fleet: a sensor silent since February read as fresh).
    """
    if isinstance(measurements, Mapping):
        return _reading_timestamp(measurements, fallback=wall)
    return wall


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
