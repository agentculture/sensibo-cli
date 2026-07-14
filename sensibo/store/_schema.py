"""DDL for the local time-series store.

Two tables:

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
    "History retention"). Kept here, not in a separate file, so one db is the
    whole story an operator can query offline.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

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


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables/indexes if they don't exist yet. Safe to call every connect."""
    with conn:
        conn.execute(_CREATE_LOCATIONS)
        conn.execute(_CREATE_READINGS)
        conn.execute(_CREATE_META)
        conn.execute(_CREATE_READINGS_LOCATION_FIELD_TIME_INDEX)
        conn.execute(_CREATE_READINGS_TIMESTAMP_INDEX)
        # SCHEMA_VERSION is an internal int constant, never caller input;
        # PRAGMA doesn't accept `?` placeholders, so this is the sanctioned way.
        conn.execute("PRAGMA user_version = " + str(SCHEMA_VERSION))
