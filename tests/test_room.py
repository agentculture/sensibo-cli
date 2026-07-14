"""Tests for sensibo.store.rooms — the room naming registry's resolver (task t14).

Written first (TDD): these fail against an empty ``sensibo/store/rooms.py``
and pass once ``resolve_location()`` / ``is_stale()`` land. Every test opens a
:class:`~sensibo.store.Store` against a ``tmp_path`` file — never the real
``~/.sensibo`` — matching ``tests/test_store.py``'s convention.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sensibo.store import KIND_POD, KIND_ROOM_SENSOR, Store
from sensibo.store.rooms import (
    DEFAULT_STALE_AFTER_HOURS,
    AmbiguousLocationError,
    LocationNotFoundError,
    is_stale,
    resolve_location,
)


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(db_path=tmp_path / "rooms.db")
    yield s
    s.close()


# --- resolve by stable id ----------------------------------------------------


def test_resolve_by_stable_id(store: Store) -> None:
    store.upsert_location("pod-1", kind=KIND_POD, product_model="airq", room_name="Office")
    loc = resolve_location(store, "pod-1")
    assert loc.id == "pod-1"


# --- resolve by operator alias ------------------------------------------------


def test_resolve_by_alias(store: Store) -> None:
    store.upsert_location("pod-2", kind=KIND_POD, product_model="airq", room_name="Office")
    store.set_alias("pod-2", "Kids Room")
    loc = resolve_location(store, "Kids Room")
    assert loc.id == "pod-2"


# --- resolve by Sensibo's own room name ---------------------------------------


def test_resolve_by_sensibo_room_name(store: Store) -> None:
    store.upsert_location("pod-3", kind=KIND_POD, product_model="airq", room_name="Living Room")
    loc = resolve_location(store, "Living Room")
    assert loc.id == "pod-3"


# --- aliases win over Sensibo room names on collision -------------------------


def test_alias_wins_over_room_name_on_collision(store: Store) -> None:
    # pod-4's Sensibo room name is "Den"; pod-5's operator alias is also "Den".
    # Resolving "Den" must return the alias match, not the room-name match.
    store.upsert_location("pod-4", kind=KIND_POD, product_model="airq", room_name="Den")
    store.upsert_location("pod-5", kind=KIND_POD, product_model="elements", room_name="Office")
    store.set_alias("pod-5", "Den")

    loc = resolve_location(store, "Den")
    assert loc.id == "pod-5"


# --- ambiguity: same tier, multiple matches ------------------------------------


def test_ambiguous_room_name_raises_listing_candidates(store: Store) -> None:
    store.upsert_location("pod-6", kind=KIND_POD, product_model="airq", room_name="Bedroom")
    store.upsert_location("ms_1", kind=KIND_ROOM_SENSOR, parent_pod_id="pod-6", room_name="Bedroom")
    with pytest.raises(AmbiguousLocationError) as exc_info:
        resolve_location(store, "Bedroom")
    ids = {loc.id for loc in exc_info.value.candidates}
    assert ids == {"pod-6", "ms_1"}
    assert "pod-6" in str(exc_info.value)
    assert "ms_1" in str(exc_info.value)


def test_ambiguous_alias_raises(store: Store) -> None:
    store.upsert_location("pod-7", kind=KIND_POD, product_model="airq")
    store.upsert_location("pod-8", kind=KIND_POD, product_model="airq")
    store.set_alias("pod-7", "Same Name")
    store.set_alias("pod-8", "Same Name")
    with pytest.raises(AmbiguousLocationError):
        resolve_location(store, "Same Name")


# --- unknown name --------------------------------------------------------------


def test_unknown_name_raises_not_found_listing_known_ids(store: Store) -> None:
    store.upsert_location("pod-9", kind=KIND_POD, product_model="airq")
    with pytest.raises(LocationNotFoundError) as exc_info:
        resolve_location(store, "Nonexistent Room")
    assert "pod-9" in exc_info.value.known
    assert "pod-9" in str(exc_info.value)


def test_unknown_name_on_empty_store_raises_not_found(store: Store) -> None:
    with pytest.raises(LocationNotFoundError) as exc_info:
        resolve_location(store, "anything")
    assert exc_info.value.known == ()


# --- rename-then-query continuity: the core acceptance for t14 -----------------


def test_rename_then_query_reaches_the_same_historical_rows(store: Store) -> None:
    store.upsert_location("pod-10", kind=KIND_POD, product_model="airq", room_name="Office")
    store.record_reading("pod-10", "temperature", 21.5, timestamp=1000.0)
    store.record_reading("pod-10", "temperature", 22.0, timestamp=2000.0)

    store.set_alias("pod-10", "Home Office")

    loc = resolve_location(store, "Home Office")
    assert loc.id == "pod-10"
    rows = store.query_range(loc.id, "temperature")
    assert [r.value for r in rows] == [21.5, 22.0]

    # The old Sensibo room name still resolves too — renaming set an alias,
    # it did not erase the vendor name (just got outranked by it).
    loc_by_old_name = resolve_location(store, "Office")
    assert loc_by_old_name.id == "pod-10"


def test_room_sensor_rename_reaches_its_own_history_not_the_parent_pods(store: Store) -> None:
    store.upsert_location("pod-11", kind=KIND_POD, product_model="airq", room_name="Living Room")
    store.upsert_location(
        "ms_11", kind=KIND_ROOM_SENSOR, parent_pod_id="pod-11", room_name="Bedroom"
    )
    store.record_reading("pod-11", "temperature", 24.0, timestamp=100.0)
    store.record_reading("ms_11", "temperature", 19.0, timestamp=100.0)

    store.set_alias("ms_11", "Nursery")

    loc = resolve_location(store, "Nursery")
    assert loc.id == "ms_11"
    rows = store.query_range(loc.id, "temperature")
    assert [r.value for r in rows] == [19.0]


# --- staleness: a pure, clock-injectable function -------------------------------


def test_is_stale_true_when_older_than_threshold() -> None:
    now = 1_000_000.0
    five_months_ago = now - (150 * 86400)
    assert is_stale(five_months_ago, stale_after_hours=24, now=now) is True


def test_is_stale_false_when_within_threshold() -> None:
    now = 1_000_000.0
    one_hour_ago = now - 3600.0
    assert is_stale(one_hour_ago, stale_after_hours=24, now=now) is False


def test_is_stale_boundary_is_exclusive(store: Store) -> None:
    now = 1_000_000.0
    exactly_at_threshold = now - (24 * 3600.0)
    assert is_stale(exactly_at_threshold, stale_after_hours=24, now=now) is False


def test_is_stale_true_when_never_seen() -> None:
    assert is_stale(None, stale_after_hours=24, now=1_000_000.0) is True


def test_is_stale_defaults_to_the_real_clock_when_now_is_omitted() -> None:
    import time

    assert is_stale(time.time(), stale_after_hours=24) is False
    assert is_stale(time.time() - 1000 * 3600, stale_after_hours=24) is True


def test_default_stale_after_is_24_hours() -> None:
    assert DEFAULT_STALE_AFTER_HOURS == 24.0
