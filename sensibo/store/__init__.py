"""sensibo.store — the local time-series retention layer.

This is sensibo-cli's retention thesis made concrete: every sensor reading
that Sensibo's cloud ever hands back lands here, on the operator's own
machine, in a plain sqlite3 file — queryable offline, retained for at least
:data:`DEFAULT_RETENTION_DAYS` (two years by default).

**Layering rule:** this package is a pure storage layer. It must never import
from ``sensibo.cli`` or ``sensibo.api`` — verbs and the HTTP client depend on
the store, never the reverse. It is stdlib-only (``sqlite3``, ``os``,
``time``, ``datetime``, ``pathlib``, ``dataclasses``) — no runtime
dependencies, matching the rest of the project.

Two Sensibo-specific traps this package encodes directly in its API (see
``docs/sensibo-api.md`` for the evidence):

* **pm25 is polymorphic.** :func:`derive_unit` branches on ``product_model``
  so the same JSON key never gets one unit tag on a Pure pod and a different,
  silently wrong one on an Elements pod.
* **A Room Sensor is not a pod.** :class:`LocationRecord` models both kinds
  of sensing location uniformly, with ``parent_pod_id`` carrying the nesting
  relationship for Room Sensors (which have no pod id of their own).

Public API
----------

* :class:`Store` — open/close a db, record locations and readings, query.
* :class:`LocationRecord`, :class:`ReadingRecord` — the two row shapes.
* :data:`DEFAULT_RETENTION_DAYS` — the retention-window default.
* :func:`default_db_path` — where the db lives absent an explicit override.
* :func:`derive_unit` — the unit-tagging rule readings are stored under.
"""

from __future__ import annotations

from ._paths import default_db_path, resolve_db_path
from ._units import derive_unit
from .store import (
    DEFAULT_RETENTION_DAYS,
    KIND_POD,
    KIND_ROOM_SENSOR,
    LocationRecord,
    ReadingRecord,
    Store,
)

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "KIND_POD",
    "KIND_ROOM_SENSOR",
    "LocationRecord",
    "ReadingRecord",
    "Store",
    "default_db_path",
    "derive_unit",
    "resolve_db_path",
]
