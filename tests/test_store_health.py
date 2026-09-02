"""Tests for the schema v2 additions: health, transitions, notifications.

Written first (TDD): every test here fails against the v1 store and passes
once :mod:`sensibo.store._schema` reaches ``SCHEMA_VERSION = 2`` and
:class:`~sensibo.store.Store` grows the health/transition/notification API.

Like ``tests/test_store.py``, every test opens a store against ``tmp_path``
— never the real ``~/.sensibo``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sensibo.store import (
    HealthRecord,
    NotificationRecord,
    Store,
    StoreVersionError,
    TransitionRecord,
    _schema,
    derive_unit,
)
from tests._fixtures_fleet import AIRQ_POD, LIVE_ROOM_SENSOR, POD_ID


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(db_path=tmp_path / "sensibo.db")
    yield s
    s.close()


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _table_names(store: Store) -> set[str]:
    rows = store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


# --- criterion 1: version + tables ------------------------------------------


def test_schema_version_is_two() -> None:
    assert _schema.SCHEMA_VERSION == 2


def test_init_schema_creates_the_v2_tables(store: Store) -> None:
    assert {"health", "transitions", "notifications"} <= _table_names(store)


def test_v2_tables_have_the_agreed_columns(store: Store) -> None:
    def columns(table: str) -> set[str]:
        rows = store._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"] for row in rows}

    assert columns("health") == {
        "location_id",
        "status",
        "since",
        "last_ok",
        "parent_pod_id",
    }
    assert columns("transitions") == {
        "id",
        "location_id",
        "from_status",
        "to_status",
        "at",
        "notified_at",
    }
    assert columns("notifications") == {
        "id",
        "kind",
        "location_id",
        "sent_at",
        "transport",
        "outcome",
    }


def test_health_primary_key_is_location_id(store: Store) -> None:
    rows = store._conn.execute("PRAGMA table_info(health)").fetchall()
    pk = {row["name"] for row in rows if row["pk"]}
    assert pk == {"location_id"}


def test_opening_a_fresh_store_stamps_the_current_version(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    with Store(db_path=path):
        pass
    assert _user_version(path) == 2


def test_a_v1_file_is_upgraded_in_place(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    with Store(db_path=path) as store:
        assert {"health", "transitions", "notifications"} <= _table_names(store)
    assert _user_version(path) == 2


def test_reopening_a_current_store_does_not_restamp_or_fail(tmp_path: Path) -> None:
    path = tmp_path / "again.db"
    with Store(db_path=path) as store:
        store.upsert_location("pod-1", kind="pod")
    with Store(db_path=path) as store:
        assert store.get_location("pod-1") is not None
    assert _user_version(path) == 2


# --- criterion 2: fail closed on a newer file -------------------------------


def test_opening_a_newer_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()

    with pytest.raises(StoreVersionError) as excinfo:
        Store(db_path=path)

    err = excinfo.value
    assert err.found == 99
    assert err.supported == 2
    assert "99" in str(err)
    assert err.remediation


def test_store_version_error_is_exported_and_is_an_exception() -> None:
    assert issubclass(StoreVersionError, Exception)


# --- criterion 3: batteryVoltage unit + guarded backfill --------------------


def test_derive_unit_battery_voltage_is_millivolts() -> None:
    assert derive_unit("batteryVoltage", "motion_sensor") == "mV"
    assert derive_unit("batteryVoltage", "airq") == "mV"
    assert derive_unit("batteryVoltage", None) == "mV"


def test_backfill_units_v2_tags_legacy_rows_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with Store(db_path=path) as store:
        # Simulate rows written by a v1 binary, which had no batteryVoltage
        # unit rule and so stored NULL.
        with store._conn:
            store._conn.execute(
                "INSERT INTO readings "
                "(location_id, field, timestamp, value_numeric, value_text, unit) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                ("ms_aaa111", "batteryVoltage", 1_000.0, 2950.0, None),
            )
            # Clear the guard __init__ already set, so the backfill runs here.
            store._conn.execute("DELETE FROM meta WHERE key = 'units_backfill_v2'")

        assert store.backfill_units_v2() == 1
        row = store.latest_reading("ms_aaa111", "batteryVoltage")
        assert row is not None and row.unit == "mV"

        # Second run is a no-op: the meta guard short-circuits it.
        assert store.backfill_units_v2() == 0
        assert store.get_meta("units_backfill_v2") is not None


def test_backfill_runs_automatically_on_open(tmp_path: Path) -> None:
    path = tmp_path / "auto.db"
    with Store(db_path=path):
        pass
    with Store(db_path=path) as store:
        assert store.get_meta("units_backfill_v2") is not None


def test_backfill_leaves_an_explicit_unit_alone(tmp_path: Path) -> None:
    path = tmp_path / "explicit.db"
    with Store(db_path=path) as store:
        with store._conn:
            store._conn.execute(
                "INSERT INTO readings "
                "(location_id, field, timestamp, value_numeric, value_text, unit) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                ("ms_aaa111", "batteryVoltage", 1_000.0, 3.0, "V"),
            )
            store._conn.execute("DELETE FROM meta WHERE key = 'units_backfill_v2'")
        assert store.backfill_units_v2() == 0
        row = store.latest_reading("ms_aaa111", "batteryVoltage")
        assert row is not None and row.unit == "V"


# --- criterion 4: health / transitions / notifications API ------------------


def test_set_and_get_health(store: Store) -> None:
    store.set_health("ms_aaa111", status="ok", since=100.0, last_ok=100.0)
    record = store.get_health("ms_aaa111")
    assert record == HealthRecord(
        location_id="ms_aaa111",
        status="ok",
        since=100.0,
        last_ok=100.0,
        parent_pod_id=None,
    )


def test_get_health_is_none_for_an_unknown_location(store: Store) -> None:
    assert store.get_health("nope") is None


def test_set_health_upserts_rather_than_duplicating(store: Store) -> None:
    store.set_health("ms_aaa111", status="ok", since=100.0, last_ok=100.0)
    store.set_health("ms_aaa111", status="stale", since=200.0, last_ok=150.0, parent_pod_id=POD_ID)
    record = store.get_health("ms_aaa111")
    assert record is not None
    assert (record.status, record.since, record.last_ok) == ("stale", 200.0, 150.0)
    assert record.parent_pod_id == POD_ID
    assert len(store.list_health()) == 1


def test_list_health_returns_every_location_ordered(store: Store) -> None:
    store.set_health("b", status="ok", since=1.0, last_ok=1.0)
    store.set_health("a", status="stale", since=2.0, last_ok=0.0)
    assert [h.location_id for h in store.list_health()] == ["a", "b"]


def test_record_transition_returns_an_id_and_lists_back(store: Store) -> None:
    tid = store.record_transition("ms_aaa111", "ok", "stale", 500.0)
    assert isinstance(tid, int)
    listed = store.list_transitions()
    assert listed == [
        TransitionRecord(
            id=tid,
            location_id="ms_aaa111",
            from_status="ok",
            to_status="stale",
            at=500.0,
            notified_at=None,
        )
    ]


def test_list_transitions_filters_by_location_and_since(store: Store) -> None:
    store.record_transition("a", "ok", "stale", 100.0)
    store.record_transition("a", "stale", "ok", 300.0)
    store.record_transition("b", "ok", "stale", 200.0)

    assert [t.at for t in store.list_transitions(location_id="a")] == [100.0, 300.0]
    assert [t.location_id for t in store.list_transitions(since=200.0)] == ["b", "a"]
    assert [t.at for t in store.list_transitions(location_id="a", since=200.0)] == [300.0]


def test_mark_transition_notified(store: Store) -> None:
    tid = store.record_transition("a", "ok", "stale", 100.0)
    store.mark_transition_notified(tid, 150.0)
    (transition,) = store.list_transitions()
    assert transition.notified_at == 150.0


def test_record_notification_returns_an_id_and_lists_back(store: Store) -> None:
    nid = store.record_notification(
        kind="stale", location_id="a", sent_at=900.0, transport="webhook", outcome="ok"
    )
    assert isinstance(nid, int)
    assert store.list_notifications() == [
        NotificationRecord(
            id=nid,
            kind="stale",
            location_id="a",
            sent_at=900.0,
            transport="webhook",
            outcome="ok",
        )
    ]


def test_list_notifications_filters_by_location_and_since(store: Store) -> None:
    store.record_notification(
        kind="stale", location_id="a", sent_at=100.0, transport="webhook", outcome="ok"
    )
    store.record_notification(
        kind="stale", location_id="b", sent_at=200.0, transport="webhook", outcome="failed"
    )
    store.record_notification(
        kind="recovered", location_id="a", sent_at=300.0, transport="webhook", outcome="ok"
    )

    assert [n.sent_at for n in store.list_notifications(location_id="a")] == [100.0, 300.0]
    assert [n.location_id for n in store.list_notifications(since=200.0)] == ["b", "a"]


# --- criterion 5: prune scope ----------------------------------------------


def test_prune_leaves_health_transitions_and_notifications_alone(store: Store) -> None:
    now = 2_000_000_000.0
    ancient = now - (900 * 86400)

    store.upsert_location("pod-1", kind="pod")
    store.record_reading("pod-1", "temperature", 20.0, timestamp=ancient)
    store.set_health("pod-1", status="ok", since=ancient, last_ok=ancient)
    store.record_transition("pod-1", "ok", "stale", ancient)
    store.record_notification(
        kind="stale", location_id="pod-1", sent_at=ancient, transport="webhook", outcome="ok"
    )

    assert store.prune(now=now) == 1

    assert store.query_range("pod-1", "temperature") == []
    assert store.get_health("pod-1") is not None
    assert len(store.list_transitions()) == 1
    assert len(store.list_notifications()) == 1


# --- criterion 6: the fleet fixture round-trips through record_readings -----


def test_every_fixture_measurement_lands_in_readings(store: Store) -> None:
    """Mirror how the collector calls the store, for the realistic fleet fixture."""
    timestamp = 1_700_000_000.0
    pod_fields = {k: v for k, v in AIRQ_POD["measurements"].items() if k != "time"}
    store.upsert_location(POD_ID, kind="pod", product_model=AIRQ_POD["productModel"])
    store.record_readings(
        POD_ID, pod_fields, timestamp=timestamp, product_model=AIRQ_POD["productModel"]
    )

    latest = store.latest_readings(POD_ID)
    assert set(latest) == set(pod_fields)
    for field, value in pod_fields.items():
        stored = latest[field]
        assert stored.timestamp == timestamp
        assert stored.value == (float(value) if isinstance(value, (int, float)) else value)


def test_room_sensor_measurements_land_including_battery_voltage(store: Store) -> None:
    """A Room Sensor payload, plus the ``batteryVoltage`` field v2 tags as mV.

    The shared fixture is frozen for this task, so ``batteryVoltage`` is added
    here as the superset a real motion sensor reports.
    """
    timestamp = 1_700_000_000.0
    model = LIVE_ROOM_SENSOR["productModel"]
    fields = {k: v for k, v in LIVE_ROOM_SENSOR["measurements"].items() if k != "time"}
    fields["batteryVoltage"] = 2950

    store.upsert_location(
        LIVE_ROOM_SENSOR["id"],
        kind="room_sensor",
        product_model=model,
        parent_pod_id=POD_ID,
    )
    store.record_readings(LIVE_ROOM_SENSOR["id"], fields, timestamp=timestamp, product_model=model)

    latest = store.latest_readings(LIVE_ROOM_SENSOR["id"])
    assert set(latest) == set(fields)
    for reading in latest.values():
        assert reading.timestamp == timestamp
    assert latest["batteryVoltage"].unit == "mV"
    assert latest["battery"].unit == "%"
