"""DDL for the local time-series store.

Six tables:

``locations``
    First-class sensing locations. A location is either a **pod** (a real
    Sensibo device; stable id = the pod's own uid) or a **Room Sensor** (a BLE
    satellite with no pod id of its own; stable id = its ``ms_*`` id, and it
    always carries the id of the parent pod it's nested under — see
    ``docs/sensibo-api.md``, "Trap 2: Room Sensor is not a pod"). ``alias`` is
    reserved for an operator-chosen name a later task will let the user set;
    the column exists from day one so naming needs no migration, and it is
    never touched by the metadata refresh path (:func:`UPSERT_LOCATION_SQL`
    doesn't list it) — only an explicit alias write updates it.

``readings``
    Field-flexible (EAV-shaped) sensor history. ``(location_id, field,
    timestamp)`` is the primary key, so re-collecting the same instant is an
    upsert, not a duplicate row. A pod reporting a field this schema has never
    seen before needs no migration — it's just a new value of the ``field``
    column. ``value_numeric``/``value_text`` are exclusive; ``unit`` is a free
    text tag (see :mod:`sensibo.store._units`), left ``NULL`` when unknown.

``meta``
    A tiny string key/value side-table for store-level facts that aren't
    readings — currently the collector's empirically probed
    ``historicalMeasurements`` backfill window (``docs/sensibo-api.md``,
    "History retention"), plus one-off migration guards such as
    ``units_backfill_v2``. Kept here, not in a separate file, so one db is the
    whole story an operator can query offline.

``health``
    One row per sensing location: its current health ``status``, the instant
    it entered that status (``since``), the last instant it was known good
    (``last_ok``), and — for a Room Sensor — the ``parent_pod_id`` it hangs
    off. Current state only; the history of state *changes* lives next door.

``transitions``
    Append-only log of health status changes (``from_status`` to
    ``to_status`` at ``at``). ``notified_at`` is stamped once an alert for
    that transition has actually gone out, so a restart can't re-announce a
    transition that was already notified.

``notifications``
    Append-only log of alerts that were attempted: what ``kind``, for which
    location, when, over which ``transport``, and with what ``outcome``.

Version policy (why the stamp is conditional)
---------------------------------------------

``PRAGMA user_version`` is the store's schema version, and
:func:`init_schema` now treats it as a **fail-closed** gate rather than
something to overwrite unconditionally:

* **v2 binary, v1 file** — the ``CREATE TABLE IF NOT EXISTS`` statements add
  the new tables and the version is stamped up to 2: an in-place upgrade
  with no data movement.
* **v2 binary, current file** — nothing to create, and the stamp is skipped
  because the file already reads :data:`SCHEMA_VERSION`.
* **v2 binary, newer (v3+) file** — :class:`StoreVersionError`. Refusing to
  open beats writing rows a future schema would misread.

The hazard this removes is the *old* unconditional ``PRAGMA user_version =
1``: during a v2 rollout, a still-running v1 daemon opening the
already-migrated file would silently stamp it back down to 1, so a later v2
open would "upgrade" a file that was never really v1. Only ever raising the
number, never lowering it, is what makes the two binaries safe to overlap.
(A v1 binary will still happily *write readings* into a v2 file, and that is
fine — the v1 tables are unchanged; it simply ignores the v2 ones.)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SCHEMA_VERSION = 2


@dataclass
class StoreVersionError(Exception):
    """The db file's schema version is newer than this build understands.

    A store-layer exception (like the ones in :mod:`sensibo.store.rooms`), so
    the storage layer never imports :class:`sensibo.cli._errors.CliError`; the
    CLI layer decides the exit code and prints :attr:`remediation`.
    """

    found: int
    supported: int
    path: str = ""

    def __post_init__(self) -> None:
        where = f" ({self.path})" if self.path else ""
        super().__init__(
            f"store schema version {self.found} is newer than the supported "
            f"version {self.supported}{where}"
        )

    @property
    def remediation(self) -> str:
        """What an operator (or agent) should do about it."""
        return (
            f"upgrade sensibo-cli to a build that understands schema version {self.found}, "
            "or point SENSIBO_DB at a different database file"
        )


_CREATE_LOCATIONS = """
CREATE TABLE IF NOT EXISTS locations (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('pod', 'room_sensor')),
    product_model TEXT,
    parent_pod_id TEXT,
    room_name     TEXT,
    alias         TEXT,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL
)
"""

_CREATE_READINGS = """
CREATE TABLE IF NOT EXISTS readings (
    location_id   TEXT NOT NULL,
    field         TEXT NOT NULL,
    timestamp     REAL NOT NULL,
    value_numeric REAL,
    value_text    TEXT,
    unit          TEXT,
    PRIMARY KEY (location_id, field, timestamp)
)
"""

_CREATE_READINGS_LOCATION_FIELD_TIME_INDEX = """
CREATE INDEX IF NOT EXISTS idx_readings_location_field_timestamp
    ON readings (location_id, field, timestamp)
"""

_CREATE_READINGS_TIMESTAMP_INDEX = """
CREATE INDEX IF NOT EXISTS idx_readings_timestamp
    ON readings (timestamp)
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_CREATE_HEALTH = """
CREATE TABLE IF NOT EXISTS health (
    location_id   TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    since         REAL NOT NULL,
    last_ok       REAL,
    parent_pod_id TEXT
)
"""

_CREATE_TRANSITIONS = """
CREATE TABLE IF NOT EXISTS transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    at          REAL NOT NULL,
    notified_at REAL
)
"""

_CREATE_TRANSITIONS_LOCATION_AT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_transitions_location_at
    ON transitions (location_id, at)
"""

_CREATE_NOTIFICATIONS = """
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    location_id TEXT,
    sent_at     REAL NOT NULL,
    transport   TEXT NOT NULL,
    outcome     TEXT NOT NULL
)
"""

_CREATE_NOTIFICATIONS_LOCATION_SENT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_notifications_location_sent_at
    ON notifications (location_id, sent_at)
"""

UPSERT_LOCATION_SQL = """
INSERT INTO locations
    (id, kind, product_model, parent_pod_id, room_name, first_seen, last_seen)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    kind = excluded.kind,
    product_model = COALESCE(excluded.product_model, locations.product_model),
    parent_pod_id = COALESCE(excluded.parent_pod_id, locations.parent_pod_id),
    room_name = COALESCE(excluded.room_name, locations.room_name),
    last_seen = excluded.last_seen
"""
# Note: `alias` is deliberately absent above — a metadata refresh from a
# fresh API poll must never clobber an operator-chosen name.

SET_ALIAS_SQL = "UPDATE locations SET alias = ? WHERE id = ?"

SELECT_LOCATION_SQL = (
    "SELECT id, kind, product_model, parent_pod_id, room_name, alias, "
    "first_seen, last_seen FROM locations WHERE id = ?"
)

SELECT_ALL_LOCATIONS_SQL = (
    "SELECT id, kind, product_model, parent_pod_id, room_name, alias, "
    "first_seen, last_seen FROM locations ORDER BY id"
)

UPSERT_READING_SQL = """
INSERT INTO readings (location_id, field, timestamp, value_numeric, value_text, unit)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(location_id, field, timestamp) DO UPDATE SET
    value_numeric = excluded.value_numeric,
    value_text = excluded.value_text,
    unit = excluded.unit
"""

SELECT_LATEST_READING_SQL = (
    "SELECT location_id, field, timestamp, value_numeric, value_text, unit FROM readings "
    "WHERE location_id = ? AND field = ? ORDER BY timestamp DESC LIMIT 1"
)

SELECT_LATEST_READINGS_SQL = (
    "SELECT r.location_id, r.field, r.timestamp, r.value_numeric, r.value_text, r.unit "
    "FROM readings r "
    "JOIN ("
    "  SELECT field, MAX(timestamp) AS timestamp FROM readings "
    "  WHERE location_id = ? GROUP BY field"
    ") latest ON r.field = latest.field AND r.timestamp = latest.timestamp "
    "WHERE r.location_id = ?"
)

DELETE_OLDER_THAN_SQL = "DELETE FROM readings WHERE timestamp < ?"

SET_META_SQL = """
INSERT INTO meta (key, value) VALUES (?, ?)
ON CONFLICT(key) DO UPDATE SET value = excluded.value
"""

GET_META_SQL = "SELECT value FROM meta WHERE key = ?"

#: One-off v2 migration: rows written by a v1 binary have no unit tag for
#: ``batteryVoltage`` (v1's `_units` map didn't know the field). Only NULL
#: units are touched, so an explicitly-tagged row is never rewritten.
BACKFILL_BATTERY_VOLTAGE_UNIT_SQL = (
    "UPDATE readings SET unit = 'mV' WHERE field = 'batteryVoltage' AND unit IS NULL"
)

#: ``meta`` key guarding :data:`BACKFILL_BATTERY_VOLTAGE_UNIT_SQL`.
UNITS_BACKFILL_V2_KEY = "units_backfill_v2"

UPSERT_HEALTH_SQL = """
INSERT INTO health (location_id, status, since, last_ok, parent_pod_id)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(location_id) DO UPDATE SET
    status = excluded.status,
    since = excluded.since,
    last_ok = excluded.last_ok,
    parent_pod_id = COALESCE(excluded.parent_pod_id, health.parent_pod_id)
"""

SELECT_HEALTH_SQL = (
    "SELECT location_id, status, since, last_ok, parent_pod_id FROM health WHERE location_id = ?"
)

SELECT_ALL_HEALTH_SQL = (
    "SELECT location_id, status, since, last_ok, parent_pod_id FROM health ORDER BY location_id"
)

INSERT_TRANSITION_SQL = (
    "INSERT INTO transitions (location_id, from_status, to_status, at, notified_at) "
    "VALUES (?, ?, ?, ?, NULL)"
)

MARK_TRANSITION_NOTIFIED_SQL = "UPDATE transitions SET notified_at = ? WHERE id = ?"

INSERT_NOTIFICATION_SQL = (
    "INSERT INTO notifications (kind, location_id, sent_at, transport, outcome) "
    "VALUES (?, ?, ?, ?, ?)"
)


def init_schema(conn: sqlite3.Connection, *, path: str = "") -> None:
    """Create tables/indexes if absent, and gate the file's schema version.

    Safe to call on every connect. Raises :class:`StoreVersionError` when the
    file was written by a newer build (see the module docstring's version
    policy); the version stamp is only ever raised, never lowered.
    """
    found = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if found > SCHEMA_VERSION:
        raise StoreVersionError(found=found, supported=SCHEMA_VERSION, path=path)
    with conn:
        conn.execute(_CREATE_LOCATIONS)
        conn.execute(_CREATE_READINGS)
        conn.execute(_CREATE_META)
        conn.execute(_CREATE_HEALTH)
        conn.execute(_CREATE_TRANSITIONS)
        conn.execute(_CREATE_NOTIFICATIONS)
        conn.execute(_CREATE_READINGS_LOCATION_FIELD_TIME_INDEX)
        conn.execute(_CREATE_READINGS_TIMESTAMP_INDEX)
        conn.execute(_CREATE_TRANSITIONS_LOCATION_AT_INDEX)
        conn.execute(_CREATE_NOTIFICATIONS_LOCATION_SENT_INDEX)
        if found < SCHEMA_VERSION:
            # SCHEMA_VERSION is an internal int constant, never caller input;
            # PRAGMA doesn't accept `?` placeholders, so this is the sanctioned way.
            conn.execute("PRAGMA user_version = " + str(SCHEMA_VERSION))
