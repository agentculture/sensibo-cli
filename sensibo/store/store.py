"""The local time-series store: sensibo-cli's retention thesis.

A pure storage layer over stdlib ``sqlite3`` — no network code, no CLI
imports, no API-client imports. Every sensor reading Sensibo's cloud ever
hands back lands here and stays here, queryable offline, for at least
:data:`DEFAULT_RETENTION_DAYS`.

Typical use::

    from sensibo.store import Store

    with Store() as store:
        store.upsert_location("pod-abc", kind="pod", product_model="airq")
        store.record_readings("pod-abc", {"temperature": 21.5, "humidity": 44})
        latest = store.latest_readings("pod-abc")

See the module docstring in :mod:`sensibo.store._schema` for the table shapes
and :mod:`sensibo.store._units` for how unit tags (including the pm25
polymorphism trap) are derived.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Iterable, Mapping

from . import _schema
from ._paths import resolve_db_path
from ._units import derive_unit

#: Retention floor for the product's "two years of history" thesis.
#: `Store.prune()` defaults to this; callers may pass a shorter or longer
#: window, but the default must never regress below two years.
DEFAULT_RETENTION_DAYS = 730

#: Location kinds. A pod is a real Sensibo device; a Room Sensor is a BLE
#: satellite nested under a parent pod's `motionSensors[]` (it is NOT a pod —
#: docs/sensibo-api.md, "Trap 2").
KIND_POD = "pod"
KIND_ROOM_SENSOR = "room_sensor"

Timestamp = float | int | datetime.datetime


@dataclass(frozen=True)
class LocationRecord:
    """A sensing location: a pod, or a Room Sensor nested under one.

    ``id`` is the stable key readings are joined on for the location's
    lifetime — a pod's own uid, or a Room Sensor's ``ms_*`` id. ``alias`` is
    an operator-chosen name (set via :meth:`Store.set_alias`); it never
    affects how readings are keyed, so renaming never rewrites history.
    """

    id: str
    kind: str
    product_model: str | None
    parent_pod_id: str | None
    room_name: str | None
    alias: str | None
    first_seen: float
    last_seen: float


@dataclass(frozen=True)
class ReadingRecord:
    """One field's value at one instant, for one location."""

    location_id: str
    field: str
    timestamp: float
    value: float | str
    unit: str | None


@dataclass(frozen=True)
class HealthRecord:
    """A location's *current* health state (one row per location).

    ``since`` is when the location entered ``status``; ``last_ok`` is the
    last instant it was known good (``None`` if it never has been).
    ``parent_pod_id`` carries the nesting for a Room Sensor, so an alert can
    name the pod a dead satellite hangs off without a second lookup.
    """

    location_id: str
    status: str
    since: float
    last_ok: float | None
    parent_pod_id: str | None


@dataclass(frozen=True)
class TransitionRecord:
    """One health status change, from the append-only ``transitions`` log.

    ``from_status`` is ``None`` for a location's very first observed state.
    ``notified_at`` stays ``None`` until an alert for this transition has
    actually gone out (:meth:`Store.mark_transition_notified`) — that is what
    keeps a restart from re-announcing history.
    """

    id: int
    location_id: str
    from_status: str | None
    to_status: str
    at: float
    notified_at: float | None


@dataclass(frozen=True)
class NotificationRecord:
    """One attempted alert, from the append-only ``notifications`` log.

    Recorded whatever the ``outcome`` — a delivery that failed is exactly
    what an operator needs to be able to see afterwards.
    """

    id: int
    kind: str
    location_id: str | None
    sent_at: float
    transport: str
    outcome: str


def _normalize_timestamp(value: Timestamp | None) -> float:
    """Coerce a caller-supplied timestamp to epoch seconds (UTC).

    ``None`` means "now". A naive :class:`datetime.datetime` is assumed UTC.
    """
    if value is None:
        return time.time()
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.timestamp()
    return float(value)


def _coerce_value(value: Any) -> tuple[float | None, str | None]:
    """Split an arbitrary reading value into (numeric, text) storage columns.

    Bools store as 0.0/1.0 numeric (Python's bool is an int subclass, and
    Sensibo's own booleans like `roomIsOccupied` are naturally 0/1-shaped).
    Anything else that isn't already numeric is stringified — the schema
    never rejects a field it hasn't seen before.
    """
    if isinstance(value, bool):
        return (1.0 if value else 0.0, None)
    if isinstance(value, (int, float)):
        return (float(value), None)
    if value is None:
        return (None, None)
    return (None, str(value))


def _restore_value(value_numeric: float | None, value_text: str | None) -> float | str:
    if value_text is not None:
        return value_text
    return value_numeric  # type: ignore[return-value]


def _row_to_location(row: sqlite3.Row) -> LocationRecord:
    return LocationRecord(
        id=row["id"],
        kind=row["kind"],
        product_model=row["product_model"],
        parent_pod_id=row["parent_pod_id"],
        room_name=row["room_name"],
        alias=row["alias"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
    )


def _row_to_reading(row: sqlite3.Row) -> ReadingRecord:
    return ReadingRecord(
        location_id=row["location_id"],
        field=row["field"],
        timestamp=row["timestamp"],
        value=_restore_value(row["value_numeric"], row["value_text"]),
        unit=row["unit"],
    )


def _row_to_health(row: sqlite3.Row) -> HealthRecord:
    return HealthRecord(
        location_id=row["location_id"],
        status=row["status"],
        since=row["since"],
        last_ok=row["last_ok"],
        parent_pod_id=row["parent_pod_id"],
    )


def _row_to_transition(row: sqlite3.Row) -> TransitionRecord:
    return TransitionRecord(
        id=row["id"],
        location_id=row["location_id"],
        from_status=row["from_status"],
        to_status=row["to_status"],
        at=row["at"],
        notified_at=row["notified_at"],
    )


def _row_to_notification(row: sqlite3.Row) -> NotificationRecord:
    return NotificationRecord(
        id=row["id"],
        kind=row["kind"],
        location_id=row["location_id"],
        sent_at=row["sent_at"],
        transport=row["transport"],
        outcome=row["outcome"],
    )


class Store:
    """A connection to the local sqlite time-series store.

    Resolves its db path via :func:`sensibo.store.resolve_db_path`: the
    ``db_path`` parameter wins if given, else ``SENSIBO_DB``, else
    ``~/.sensibo/sensibo.db``. The parent directory is created (mode 0700) on
    connect if it doesn't already exist.

    Opening raises :class:`~sensibo.store.StoreVersionError` when the file was
    written by a newer build (see :mod:`sensibo.store._schema`'s version
    policy); a v1 file is upgraded in place.
    """

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.path: Path = resolve_db_path(db_path)
        _ensure_parent_dir(self.path)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode = WAL")
        try:
            _schema.init_schema(self._conn, path=str(self.path))
        except Exception:
            self._conn.close()
            raise
        self.backfill_units_v2()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- locations ------------------------------------------------------

    def upsert_location(
        self,
        location_id: str,
        *,
        kind: str,
        product_model: str | None = None,
        parent_pod_id: str | None = None,
        room_name: str | None = None,
        seen_at: Timestamp | None = None,
    ) -> None:
        """Record or refresh a location's metadata and bump ``last_seen``.

        Never touches ``alias`` — that is only ever set via
        :meth:`set_alias`, so a routine metadata refresh from a fresh API
        poll can't clobber an operator-chosen name.
        """
        if kind not in (KIND_POD, KIND_ROOM_SENSOR):
            raise ValueError(f"unknown location kind: {kind!r}")
        ts = _normalize_timestamp(seen_at)
        with self._conn:
            self._conn.execute(
                _schema.UPSERT_LOCATION_SQL,
                (location_id, kind, product_model, parent_pod_id, room_name, ts, ts),
            )

    def set_alias(self, location_id: str, alias: str | None) -> None:
        """Set (or clear, with ``alias=None``) the operator-chosen name.

        Readings are keyed on ``location_id`` only, never on the alias, so
        this never touches — and never needs to rewrite — historical rows.
        """
        with self._conn:
            self._conn.execute(_schema.SET_ALIAS_SQL, (alias, location_id))

    def get_location(self, location_id: str) -> LocationRecord | None:
        row = self._conn.execute(_schema.SELECT_LOCATION_SQL, (location_id,)).fetchone()
        return _row_to_location(row) if row else None

    def list_locations(self) -> list[LocationRecord]:
        rows = self._conn.execute(_schema.SELECT_ALL_LOCATIONS_SQL).fetchall()
        return [_row_to_location(row) for row in rows]

    # -- readings ---------------------------------------------------------

    def record_reading(
        self,
        location_id: str,
        field: str,
        value: Any,
        *,
        timestamp: Timestamp | None = None,
        unit: str | None = None,
        product_model: str | None = None,
    ) -> None:
        """Store one field's value for a location at an instant.

        Idempotent: re-recording the same ``(location_id, field, timestamp)``
        upserts rather than duplicating a row, so re-collection after a
        restart or overlap is always safe.

        ``unit`` is derived automatically (see :mod:`sensibo.store._units`)
        when not given explicitly. Derivation needs ``product_model`` for the
        polymorphic fields (currently just ``pm25``); if not passed here, it
        falls back to whatever was last recorded for this location via
        :meth:`upsert_location`.
        """
        ts = _normalize_timestamp(timestamp)
        if unit is None:
            resolved_model = product_model
            if resolved_model is None:
                existing = self.get_location(location_id)
                resolved_model = existing.product_model if existing else None
            unit = derive_unit(field, resolved_model)
        value_numeric, value_text = _coerce_value(value)
        with self._conn:
            self._conn.execute(
                _schema.UPSERT_READING_SQL,
                (location_id, field, ts, value_numeric, value_text, unit),
            )

    def record_readings(
        self,
        location_id: str,
        values: Mapping[str, Any],
        *,
        timestamp: Timestamp | None = None,
        product_model: str | None = None,
    ) -> None:
        """Bulk variant of :meth:`record_reading` for one poll's worth of fields.

        All fields share one ``timestamp`` (defaulting to "now" once, not
        once per field) — this is what a single ``GET /users/me/pods``
        response maps onto.
        """
        ts = _normalize_timestamp(timestamp)
        resolved_model = product_model
        if resolved_model is None:
            existing = self.get_location(location_id)
            resolved_model = existing.product_model if existing else None
        rows = []
        for field, value in values.items():
            unit = derive_unit(field, resolved_model)
            value_numeric, value_text = _coerce_value(value)
            rows.append((location_id, field, ts, value_numeric, value_text, unit))
        # One transaction per poll, not one per field: each commit is an fsync
        # under WAL, and 10.8 ms/row (measured 2026-09-02) made a 7-day fixture
        # take minutes. Approved plan deviation d1.
        with self._conn:
            self._conn.executemany(_schema.UPSERT_READING_SQL, rows)

    def record_series(
        self,
        location_id: str,
        field: str,
        points: Iterable[tuple[Timestamp, Any]],
        *,
        product_model: str | None = None,
    ) -> int:
        """Bulk-record one field's time series in a single transaction.

        ``points`` is an iterable of ``(timestamp, value)`` pairs. This is the
        fast path for ``historicalMeasurements`` backfill and for tests that
        seed days of history: one ``executemany`` under one commit instead of
        one commit per reading (approved plan deviation d1). Same upsert
        semantics as :meth:`record_reading`. Returns the number of rows sent.
        """
        resolved_model = product_model
        if resolved_model is None:
            existing = self.get_location(location_id)
            resolved_model = existing.product_model if existing else None
        unit = derive_unit(field, resolved_model)
        rows = []
        for stamp, value in points:
            value_numeric, value_text = _coerce_value(value)
            rows.append(
                (location_id, field, _normalize_timestamp(stamp), value_numeric, value_text, unit)
            )
        if not rows:
            return 0
        with self._conn:
            self._conn.executemany(_schema.UPSERT_READING_SQL, rows)
        return len(rows)

    def latest_reading(self, location_id: str, field: str) -> ReadingRecord | None:
        row = self._conn.execute(_schema.SELECT_LATEST_READING_SQL, (location_id, field)).fetchone()
        return _row_to_reading(row) if row else None

    def latest_readings(self, location_id: str) -> dict[str, ReadingRecord]:
        """The most recent reading for every field this location has reported."""
        rows = self._conn.execute(
            _schema.SELECT_LATEST_READINGS_SQL, (location_id, location_id)
        ).fetchall()
        return {row["field"]: _row_to_reading(row) for row in rows}

    def query_range(
        self,
        location_id: str,
        field: str,
        *,
        since: Timestamp | None = None,
        until: Timestamp | None = None,
        limit: int | None = None,
    ) -> list[ReadingRecord]:
        """Readings for one field at one location, ordered oldest to newest.

        ``since``/``until`` are inclusive bounds; omit either (or both) for
        an open-ended range.

        ``limit``, if given, bounds the result to the ``limit`` *most
        recent* readings within that range — applied SQL-side (an inner
        ``ORDER BY timestamp DESC LIMIT ?``, re-sorted ascending by an outer
        query), never a Python-side ``fetchall()``-then-slice. That
        distinction is the fix for Qodo review 3581287838: a store holding
        months of ~90s-cadence history must never have to materialise every
        row it has ever seen just to hand a caller the most recent handful.
        ``None`` (the default) stays unbounded, so every pre-existing caller
        keeps its current behaviour unchanged.
        """
        since_ts = _normalize_timestamp(since) if since is not None else None
        until_ts = _normalize_timestamp(until) if until is not None else None
        sql, params = _build_range_query(location_id, field, since_ts, until_ts, limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_reading(row) for row in rows]

    # -- retention --------------------------------------------------------

    def prune(
        self,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        *,
        now: Timestamp | None = None,
    ) -> int:
        """Delete readings older than ``retention_days``. Returns rows deleted.

        Rows exactly at or newer than the cutoff are never touched. ``now``
        defaults to the real current time; tests pass a fixed reference
        instant for determinism.
        """
        reference = _normalize_timestamp(now)
        cutoff = reference - (retention_days * 86400)
        with self._conn:
            cursor = self._conn.execute(_schema.DELETE_OLDER_THAN_SQL, (cutoff,))
            return cursor.rowcount

    # -- metadata ---------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        """Persist a small store-level fact under ``key`` (idempotent upsert).

        Not for readings — this is the side-channel the collector uses to
        remember store-wide facts like the empirically probed
        ``historicalMeasurements`` backfill window, so a first run's finding
        survives a restart and later runs skip the probe.
        """
        with self._conn:
            self._conn.execute(_schema.SET_META_SQL, (key, value))

    def get_meta(self, key: str) -> str | None:
        """Return the value stored under ``key``, or ``None`` if unset."""
        row = self._conn.execute(_schema.GET_META_SQL, (key,)).fetchone()
        return row["value"] if row else None

    # -- migrations -------------------------------------------------------

    def backfill_units_v2(self) -> int:
        """Tag legacy ``batteryVoltage`` rows as ``mV``. Returns rows changed.

        A v1 binary had no unit rule for ``batteryVoltage`` and stored those
        readings with a ``NULL`` unit; v2 does (:mod:`sensibo.store._units`).
        This one-off pass repairs the existing rows so history and new
        readings agree, and only ever fills a ``NULL`` — an explicitly
        tagged row is left exactly as written.

        Called once from :meth:`__init__` and guarded by the ``meta`` key
        ``units_backfill_v2`` inside the same transaction as the update, so a
        second call (or a second process) changes nothing and returns ``0``.
        """
        if self.get_meta(_schema.UNITS_BACKFILL_V2_KEY) is not None:
            return 0
        with self._conn:
            cursor = self._conn.execute(_schema.BACKFILL_BATTERY_VOLTAGE_UNIT_SQL)
            changed = cursor.rowcount
            self._conn.execute(_schema.SET_META_SQL, (_schema.UNITS_BACKFILL_V2_KEY, str(changed)))
        return changed

    # -- health -----------------------------------------------------------

    def set_health(
        self,
        location_id: str,
        *,
        status: str,
        since: Timestamp,
        last_ok: Timestamp | None = None,
        parent_pod_id: str | None = None,
    ) -> None:
        """Record a location's current health state (upsert, one row per location).

        ``since`` is when the location entered ``status`` — the caller owns
        that decision, since only it knows whether this poll continued an
        existing state or started a new one. ``parent_pod_id`` is only
        needed for a Room Sensor; passing ``None`` keeps whatever is already
        stored rather than clearing it.
        """
        with self._conn:
            self._conn.execute(
                _schema.UPSERT_HEALTH_SQL,
                (
                    location_id,
                    status,
                    _normalize_timestamp(since),
                    None if last_ok is None else _normalize_timestamp(last_ok),
                    parent_pod_id,
                ),
            )

    def get_health(self, location_id: str) -> HealthRecord | None:
        """The location's current health row, or ``None`` if never recorded."""
        row = self._conn.execute(_schema.SELECT_HEALTH_SQL, (location_id,)).fetchone()
        return _row_to_health(row) if row else None

    def list_health(self) -> list[HealthRecord]:
        """Every location's current health row, ordered by location id."""
        rows = self._conn.execute(_schema.SELECT_ALL_HEALTH_SQL).fetchall()
        return [_row_to_health(row) for row in rows]

    # -- transitions ------------------------------------------------------

    def record_transition(
        self,
        location_id: str,
        from_status: str | None,
        to_status: str,
        at: Timestamp,
    ) -> int:
        """Append a health status change. Returns the new transition's id.

        The log is append-only: a transition is a historical fact, never
        edited afterwards. The one field that does change later is
        ``notified_at`` (:meth:`mark_transition_notified`).
        """
        with self._conn:
            cursor = self._conn.execute(
                _schema.INSERT_TRANSITION_SQL,
                (location_id, from_status, to_status, _normalize_timestamp(at)),
            )
        return int(cursor.lastrowid)

    def list_transitions(
        self,
        location_id: str | None = None,
        since: Timestamp | None = None,
    ) -> list[TransitionRecord]:
        """Transitions, oldest first, optionally narrowed by location and/or time.

        ``since`` is an inclusive lower bound on ``at``. Both filters are
        applied SQL-side, so a long-lived store never materialises its whole
        transition history to answer a narrow question.
        """
        since_ts = _normalize_timestamp(since) if since is not None else None
        sql, params = _build_log_query(
            "SELECT id, location_id, from_status, to_status, at, notified_at FROM transitions",
            time_column="at",
            location_id=location_id,
            since_ts=since_ts,
        )
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_transition(row) for row in rows]

    def mark_transition_notified(self, transition_id: int, notified_at: Timestamp) -> None:
        """Stamp when an alert for this transition actually went out.

        Until this is set, the transition is still owed a notification — that
        is what makes the alerting path safe to resume after a restart.
        """
        with self._conn:
            self._conn.execute(
                _schema.MARK_TRANSITION_NOTIFIED_SQL,
                (_normalize_timestamp(notified_at), transition_id),
            )

    # -- notifications ----------------------------------------------------

    def record_notification(
        self,
        *,
        kind: str,
        location_id: str | None,
        sent_at: Timestamp,
        transport: str,
        outcome: str,
    ) -> int:
        """Append an attempted alert. Returns the new notification's id.

        Every attempt is recorded regardless of ``outcome`` — a failed
        delivery is precisely what an operator needs to be able to see later.
        ``location_id`` may be ``None`` for a store-wide alert that isn't
        about one location.
        """
        with self._conn:
            cursor = self._conn.execute(
                _schema.INSERT_NOTIFICATION_SQL,
                (kind, location_id, _normalize_timestamp(sent_at), transport, outcome),
            )
        return int(cursor.lastrowid)

    def list_notifications(
        self,
        since: Timestamp | None = None,
        location_id: str | None = None,
    ) -> list[NotificationRecord]:
        """Notifications, oldest first, optionally narrowed by time and/or location.

        ``since`` is an inclusive lower bound on ``sent_at``.
        """
        since_ts = _normalize_timestamp(since) if since is not None else None
        sql, params = _build_log_query(
            "SELECT id, kind, location_id, sent_at, transport, outcome FROM notifications",
            time_column="sent_at",
            location_id=location_id,
            since_ts=since_ts,
        )
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_notification(row) for row in rows]


def _build_log_query(
    select_sql: str,
    *,
    time_column: str,
    location_id: str | None,
    since_ts: float | None,
) -> tuple[str, tuple[Any, ...]]:
    """Build the (sql, params) pair for an append-only log listing.

    ``select_sql`` and ``time_column`` are internal constants, never caller
    input; every value the caller supplies goes through a ``?`` placeholder.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if location_id is not None:
        clauses.append("location_id = ?")
        params.append(location_id)
    if since_ts is not None:
        clauses.append(f"{time_column} >= ?")
        params.append(since_ts)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return f"{select_sql}{where} ORDER BY {time_column} ASC, id ASC", tuple(params)


def _build_range_query(
    location_id: str,
    field: str,
    since_ts: float | None,
    until_ts: float | None,
    limit: int | None = None,
) -> tuple[str, tuple[Any, ...]]:
    """Build the (sql, params) pair for `query_range`.

    Each branch is a complete literal query — no runtime string
    concatenation of SQL text — so the shape stays parameterised and static.

    When `limit` is given, the *inner* query orders newest-first and caps
    there with `LIMIT ?` (SQL-side bounding), and an outer query re-sorts
    that already-bounded result set back to oldest-first — a store never
    fetches more rows than `limit` regardless of how much history exists.
    """
    if limit is not None:
        if since_ts is not None and until_ts is not None:
            sql = (
                "SELECT location_id, field, timestamp, value_numeric, value_text, unit FROM ("
                "SELECT location_id, field, timestamp, value_numeric, value_text, unit "
                "FROM readings "
                "WHERE location_id = ? AND field = ? AND timestamp >= ? AND timestamp <= ? "
                "ORDER BY timestamp DESC LIMIT ?"
                ") ORDER BY timestamp ASC"
            )
            return sql, (location_id, field, since_ts, until_ts, limit)
        if since_ts is not None:
            sql = (
                "SELECT location_id, field, timestamp, value_numeric, value_text, unit FROM ("
                "SELECT location_id, field, timestamp, value_numeric, value_text, unit "
                "FROM readings "
                "WHERE location_id = ? AND field = ? AND timestamp >= ? "
                "ORDER BY timestamp DESC LIMIT ?"
                ") ORDER BY timestamp ASC"
            )
            return sql, (location_id, field, since_ts, limit)
        if until_ts is not None:
            sql = (
                "SELECT location_id, field, timestamp, value_numeric, value_text, unit FROM ("
                "SELECT location_id, field, timestamp, value_numeric, value_text, unit "
                "FROM readings "
                "WHERE location_id = ? AND field = ? AND timestamp <= ? "
                "ORDER BY timestamp DESC LIMIT ?"
                ") ORDER BY timestamp ASC"
            )
            return sql, (location_id, field, until_ts, limit)
        sql = (
            "SELECT location_id, field, timestamp, value_numeric, value_text, unit FROM ("
            "SELECT location_id, field, timestamp, value_numeric, value_text, unit "
            "FROM readings "
            "WHERE location_id = ? AND field = ? "
            "ORDER BY timestamp DESC LIMIT ?"
            ") ORDER BY timestamp ASC"
        )
        return sql, (location_id, field, limit)

    if since_ts is not None and until_ts is not None:
        sql = (
            "SELECT location_id, field, timestamp, value_numeric, value_text, unit "
            "FROM readings "
            "WHERE location_id = ? AND field = ? AND timestamp >= ? AND timestamp <= ? "
            "ORDER BY timestamp ASC"
        )
        return sql, (location_id, field, since_ts, until_ts)
    if since_ts is not None:
        sql = (
            "SELECT location_id, field, timestamp, value_numeric, value_text, unit "
            "FROM readings "
            "WHERE location_id = ? AND field = ? AND timestamp >= ? "
            "ORDER BY timestamp ASC"
        )
        return sql, (location_id, field, since_ts)
    if until_ts is not None:
        sql = (
            "SELECT location_id, field, timestamp, value_numeric, value_text, unit "
            "FROM readings "
            "WHERE location_id = ? AND field = ? AND timestamp <= ? "
            "ORDER BY timestamp ASC"
        )
        return sql, (location_id, field, until_ts)
    sql = (
        "SELECT location_id, field, timestamp, value_numeric, value_text, unit "
        "FROM readings "
        "WHERE location_id = ? AND field = ? ORDER BY timestamp ASC"
    )
    return sql, (location_id, field)


def _ensure_parent_dir(path: Path) -> None:
    """Create ``path``'s parent directory with restrictive (0700) permissions."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    os.chmod(parent, 0o700)
