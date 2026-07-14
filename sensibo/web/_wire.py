"""JSON-safe dict shaping for locations/readings — the web API's wire format (task t12).

Deliberately mirrors ``sensibo.cli._commands.query``'s dict shaping (same
keys, same ISO formatting) so the ``/api/*`` endpoints describe the store the
same way ``sensibo query --json`` does, without an import from
:mod:`sensibo.web` into :mod:`sensibo.cli._commands` for this narrow piece —
:mod:`sensibo.web` depends on :mod:`sensibo.store` only here.
"""

from __future__ import annotations

import datetime

from sensibo.store import LocationRecord, ReadingRecord
from sensibo.store.rooms import DEFAULT_STALE_AFTER_HOURS, is_stale


def format_iso(ts: float) -> str:
    return (
        datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def display_name(loc: LocationRecord) -> str:
    """The best human name for a location: alias, then Sensibo's room name, then id."""
    return loc.alias or loc.room_name or loc.id


def location_to_dict(
    loc: LocationRecord, *, stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS
) -> dict[str, object]:
    return {
        "id": loc.id,
        "kind": loc.kind,
        "product_model": loc.product_model,
        "parent_pod_id": loc.parent_pod_id,
        "room_name": loc.room_name,
        "alias": loc.alias,
        "display_name": display_name(loc),
        "first_seen": loc.first_seen,
        "first_seen_iso": format_iso(loc.first_seen),
        "last_seen": loc.last_seen,
        "last_seen_iso": format_iso(loc.last_seen),
        "stale": is_stale(loc.last_seen, stale_after_hours=stale_after_hours),
    }


def reading_to_dict(r: ReadingRecord) -> dict[str, object]:
    return {
        "location_id": r.location_id,
        "field": r.field,
        "timestamp": r.timestamp,
        "timestamp_iso": format_iso(r.timestamp),
        "value": r.value,
        "unit": r.unit,
    }
