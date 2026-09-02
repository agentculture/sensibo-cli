"""JSON-safe dict shaping for locations/readings — the web API's wire format (task t12).

Deliberately mirrors ``sensibo.cli._commands.query``'s dict shaping (same
keys, same ISO formatting) so the ``/api/*`` endpoints describe the store the
same way ``sensibo query --json`` does, without an import from
:mod:`sensibo.web` into :mod:`sensibo.cli._commands` for this narrow piece —
:mod:`sensibo.web` depends on :mod:`sensibo.store` only here.

**Health (task t9).** ``location_to_dict`` accepts an optional
:class:`~sensibo.store.HealthRecord` (:meth:`sensibo.store.Store.get_health`).
When one exists, the wire payload carries the health table's own
``status``/``since``/``last_ok`` — the same values ``sensibo query health``
will show — alongside the derived ``stale`` flag. When there is no health row
yet (the evaluator hasn't run, or this is a fresh location), the
``health_*`` fields are ``None`` and callers fall back to ``stale``.
"""

from __future__ import annotations

import datetime

from sensibo.store import HealthRecord, LocationRecord, ReadingRecord
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
    loc: LocationRecord,
    *,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
    health: HealthRecord | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
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
        "health_status": None,
        "health_since": None,
        "health_since_iso": None,
        "health_last_ok": None,
        "health_last_ok_iso": None,
    }
    if health is not None:
        payload["health_status"] = health.status
        payload["health_since"] = health.since
        payload["health_since_iso"] = format_iso(health.since)
        payload["health_last_ok"] = health.last_ok
        payload["health_last_ok_iso"] = (
            format_iso(health.last_ok) if health.last_ok is not None else None
        )
    return payload


def reading_to_dict(r: ReadingRecord) -> dict[str, object]:
    return {
        "location_id": r.location_id,
        "field": r.field,
        "timestamp": r.timestamp,
        "timestamp_iso": format_iso(r.timestamp),
        "value": r.value,
        "unit": r.unit,
    }
