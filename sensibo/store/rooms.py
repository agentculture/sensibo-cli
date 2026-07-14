"""The room naming registry: resolve a location by id, alias, or room name.

Every sensing location — the main pod and each Room Sensor nested under it
(``docs/sensibo-api.md``, "Trap 2: Room Sensor is not a pod") — can be
addressed three ways: its stable ``id``, the operator's chosen ``alias``
(:meth:`sensibo.store.Store.set_alias`), or the ``room_name`` Sensibo itself
reports. :func:`resolve_location` is the single place that turns any of the
three into a :class:`~sensibo.store.LocationRecord`, so every verb that takes
a location argument (``room name``, and later ``read``, ``query``, rules, the
web dashboard) resolves names the same way.

**Renames never orphan history.** Readings key on the stable ``id`` only
(``sensibo/store/_schema.py``); an alias is a display-time label layered on
top. Resolving a new alias to its location's ``id`` is exactly what lets a
rename reach the same historical rows as before — there is nothing to
migrate.

This module is a pure extension of the storage layer: stdlib-only, and it
must never import from ``sensibo.cli`` or ``sensibo.api`` (same layering rule
as the rest of ``sensibo.store`` — see ``sensibo/store/__init__.py``). It
raises its own exception types rather than :class:`sensibo.cli._errors.CliError`
so callers in the CLI layer decide the exit code and remediation text.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .store import LocationRecord, Store

#: Default staleness threshold for ``sensibo room list``'s STALE flag.
DEFAULT_STALE_AFTER_HOURS = 24.0


class LocationResolutionError(Exception):
    """Base class for :func:`resolve_location` failures."""


@dataclass
class LocationNotFoundError(LocationResolutionError):
    """No location matches ``query`` by id, alias, or Sensibo room name."""

    query: str
    known: tuple[str, ...]

    def __post_init__(self) -> None:
        ids = ", ".join(self.known) if self.known else "(none)"
        super().__init__(f"no location matches {self.query!r}; known ids: {ids}")


@dataclass
class AmbiguousLocationError(LocationResolutionError):
    """``query`` matches more than one location at the same resolution tier."""

    query: str
    candidates: tuple[LocationRecord, ...]

    def __post_init__(self) -> None:
        ids = ", ".join(loc.id for loc in self.candidates)
        super().__init__(f"{self.query!r} matches multiple locations: {ids}")


def resolve_location(store: Store, name_or_id: str) -> LocationRecord:
    """Resolve a stable id, an operator alias, or a Sensibo room name to a location.

    Resolution order (first tier with any match wins):

    1. **Stable id** — exact match on ``locations.id``. Always unambiguous:
       ``id`` is the table's primary key.
    2. **Operator alias** — exact match on ``alias``
       (:meth:`Store.set_alias`). Aliases win over Sensibo's own room names on
       collision, since the alias is what the operator deliberately chose.
    3. **Sensibo room name** — exact match on ``room_name``, the vendor-reported
       name.

    Raises :class:`AmbiguousLocationError` when a tier has more than one match,
    and :class:`LocationNotFoundError` when no tier matches at all.
    """
    locations = store.list_locations()

    by_id = {loc.id: loc for loc in locations}
    if name_or_id in by_id:
        return by_id[name_or_id]

    alias_matches = tuple(loc for loc in locations if loc.alias == name_or_id)
    if len(alias_matches) == 1:
        return alias_matches[0]
    if len(alias_matches) > 1:
        raise AmbiguousLocationError(name_or_id, alias_matches)

    room_name_matches = tuple(loc for loc in locations if loc.room_name == name_or_id)
    if len(room_name_matches) == 1:
        return room_name_matches[0]
    if len(room_name_matches) > 1:
        raise AmbiguousLocationError(name_or_id, room_name_matches)

    raise LocationNotFoundError(name_or_id, tuple(sorted(by_id)))


def is_stale(
    last_seen: float | None,
    *,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
    now: float | None = None,
) -> bool:
    """True if ``last_seen`` is older than ``stale_after_hours`` ago, or unknown.

    ``now`` defaults to the real current time; tests pass a fixed reference
    instant for determinism (mirrors :meth:`Store.prune`'s ``now`` parameter).
    A location that has never been seen at all (``last_seen is None``) is
    always considered stale.
    """
    if last_seen is None:
        return True
    reference = time.time() if now is None else now
    return (reference - last_seen) > (stale_after_hours * 3600.0)
