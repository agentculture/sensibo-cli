"""Tests for sensibo.store — the local time-series retention layer.

Written first (TDD): these fail against an empty ``sensibo/store`` package and
pass once the schema + query API described in the t2 task exist.

Every test opens a :class:`~sensibo.store.Store` against a ``tmp_path`` file —
never the real ``~/.sensibo`` — per the task's hard rule.
"""

from __future__ import annotations

import ast
import socket
import time
from pathlib import Path

import pytest

from sensibo.store import (
    DEFAULT_RETENTION_DAYS,
    LocationRecord,
    ReadingRecord,
    Store,
    default_db_path,
    derive_unit,
)

# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(db_path=tmp_path / "sensibo.db")
    yield s
    s.close()


# --- schema flexibility: a never-seen field needs no migration --------------


def test_never_seen_field_stores_without_migration(store: Store) -> None:
    store.upsert_location("pod-1", kind="pod", product_model="airq", room_name="Office")
    # A field this test invents on the spot — nothing in the schema
    # special-cases it. If this requires a migration, the design is wrong.
    store.record_reading("pod-1", "totallyNovelFutureField", 42.0)

    reading = store.latest_reading("pod-1", "totallyNovelFutureField")
    assert reading is not None
    assert reading.value == 42.0


def test_bulk_record_readings_accepts_unknown_and_known_fields_together(store: Store) -> None:
    store.upsert_location("pod-2", kind="pod", product_model="airq")
    store.record_readings(
        "pod-2",
        {"temperature": 21.0, "humidity": 55.0, "somethingBrandNew": "on"},
        timestamp=1_000_000.0,
    )
    latest = store.latest_readings("pod-2")
    assert set(latest) == {"temperature", "humidity", "somethingBrandNew"}
    assert latest["somethingBrandNew"].value == "on"
    assert latest["somethingBrandNew"].unit is None


# --- pm25 polymorphism: the trap from docs/sensibo-api.md -------------------


def test_pm25_on_pure_is_tagged_aqi_enum(store: Store) -> None:
    store.upsert_location("pod-pure", kind="pod", product_model="pure")
    store.record_reading("pod-pure", "pm25", 2)
    reading = store.latest_reading("pod-pure", "pm25")
    assert reading is not None
    assert reading.value == 2
    assert reading.unit == "aqi"


def test_pm25_on_elements_is_tagged_micrograms(store: Store) -> None:
    store.upsert_location("pod-elements", kind="pod", product_model="elements")
    store.record_reading("pod-elements", "pm25", 15.4)
    reading = store.latest_reading("pod-elements", "pm25")
    assert reading is not None
    assert reading.value == 15.4
    assert reading.unit == "ug/m3"


def test_pm25_unit_derives_from_stored_location_product_model(store: Store) -> None:
    # product_model isn't re-supplied on every reading call — the store looks
    # it up from the location row that was already upserted.
    store.upsert_location("pod-pure-2", kind="pod", product_model="pure")
    store.record_reading("pod-pure-2", "pm25", 1)
    assert store.latest_reading("pod-pure-2", "pm25").unit == "aqi"


def test_derive_unit_pm25_branches_on_product_model() -> None:
    assert derive_unit("pm25", "pure") == "aqi"
    assert derive_unit("pm25", "elements") == "ug/m3"
    assert derive_unit("pm25", None) is None


def test_derive_unit_known_fields_get_sensible_tags() -> None:
    assert derive_unit("temperature", None) == "C"
    assert derive_unit("humidity", None) == "%"
    assert derive_unit("tvoc", None) == "ppb"
    assert derive_unit("co2", None) == "ppm"


def test_derive_unit_unknown_field_is_none_not_an_error() -> None:
    # No hardcoded field universe: an unrecognised field just has no unit tag.
    assert derive_unit("aBrandNewSensorFieldNobodyHasSeen", None) is None


# --- locations are first-class: pods and Room Sensors -----------------------


def test_pod_location_metadata_round_trips(store: Store) -> None:
    store.upsert_location(
        "pod-3",
        kind="pod",
        product_model="elements",
        room_name="Living Room",
    )
    loc = store.get_location("pod-3")
    assert loc == LocationRecord(
        id="pod-3",
        kind="pod",
        product_model="elements",
        parent_pod_id=None,
        room_name="Living Room",
        alias=None,
        first_seen=loc.first_seen,
        last_seen=loc.last_seen,
    )


def test_room_sensor_is_not_a_pod_and_carries_its_parent_pod_id(store: Store) -> None:
    store.upsert_location("pod-4", kind="pod", product_model="airq", room_name="Bedroom")
    store.upsert_location(
        "ms_abc123",
        kind="room_sensor",
        parent_pod_id="pod-4",
        room_name="Bedroom",
    )
    loc = store.get_location("ms_abc123")
    assert loc.kind == "room_sensor"
    assert loc.parent_pod_id == "pod-4"
    # Room Sensors have no pod id / product model of their own.
    assert loc.product_model is None


def test_list_locations_returns_every_known_location(store: Store) -> None:
    store.upsert_location("pod-5", kind="pod", product_model="pure")
    store.upsert_location("ms_xyz", kind="room_sensor", parent_pod_id="pod-5")
    ids = {loc.id for loc in store.list_locations()}
    assert ids == {"pod-5", "ms_xyz"}


def test_upsert_location_updates_last_seen_without_new_row(store: Store) -> None:
    store.upsert_location("pod-6", kind="pod", product_model="airq", seen_at=1_000.0)
    store.upsert_location("pod-6", kind="pod", product_model="airq", seen_at=2_000.0)
    locations = [loc for loc in store.list_locations() if loc.id == "pod-6"]
    assert len(locations) == 1
    assert locations[0].last_seen == 2_000.0


# --- aliasing: schema-ready for a later naming verb, no migration needed ----


def test_alias_defaults_to_none_and_can_be_set_without_touching_readings(store: Store) -> None:
    store.upsert_location("pod-7", kind="pod", product_model="airq")
    assert store.get_location("pod-7").alias is None

    store.record_reading("pod-7", "temperature", 20.0, timestamp=1.0)
    store.set_alias("pod-7", "Kids Room")

    assert store.get_location("pod-7").alias == "Kids Room"
    # Renaming must not rewrite historical rows: the reading is still keyed
    # on the stable location id, unaffected by the alias.
    reading = store.latest_reading("pod-7", "temperature")
    assert reading.location_id == "pod-7"
    assert reading.value == 20.0


def test_re_upserting_location_never_clobbers_an_existing_alias(store: Store) -> None:
    store.upsert_location("pod-8", kind="pod", product_model="airq")
    store.set_alias("pod-8", "Nursery")
    # A subsequent poll re-upserts metadata (e.g. new firmware version) but
    # never supplies an alias — it must not wipe the operator's naming.
    store.upsert_location("pod-8", kind="pod", product_model="airq", room_name="Room 2")
    assert store.get_location("pod-8").alias == "Nursery"


# --- idempotent upsert on (location_id, field, timestamp) -------------------


def test_recollecting_the_same_reading_upserts_not_duplicates(store: Store) -> None:
    store.upsert_location("pod-9", kind="pod", product_model="airq")
    store.record_reading("pod-9", "temperature", 20.0, timestamp=500.0)
    store.record_reading("pod-9", "temperature", 20.5, timestamp=500.0)  # re-collection, same ts

    rows = store.query_range("pod-9", "temperature", since=0.0, until=1000.0)
    assert len(rows) == 1
    assert rows[0].value == 20.5


# --- time-range queries -------------------------------------------------


def test_query_range_filters_by_since_and_until(store: Store) -> None:
    store.upsert_location("pod-10", kind="pod", product_model="airq")
    for ts in (100.0, 200.0, 300.0, 400.0):
        store.record_reading("pod-10", "temperature", ts / 10, timestamp=ts)

    rows = store.query_range("pod-10", "temperature", since=150.0, until=350.0)
    assert [r.timestamp for r in rows] == [200.0, 300.0]


def test_query_range_without_bounds_returns_everything_in_order(store: Store) -> None:
    store.upsert_location("pod-11", kind="pod", product_model="airq")
    store.record_reading("pod-11", "temperature", 1.0, timestamp=300.0)
    store.record_reading("pod-11", "temperature", 2.0, timestamp=100.0)
    store.record_reading("pod-11", "temperature", 3.0, timestamp=200.0)

    rows = store.query_range("pod-11", "temperature")
    assert [r.timestamp for r in rows] == [100.0, 200.0, 300.0]


def test_query_range_limit_bounds_sql_side_to_the_n_most_recent_readings(store: Store) -> None:
    store.upsert_location("pod-17", kind="pod", product_model="airq")
    for i in range(50):
        store.record_reading("pod-17", "temperature", float(i), timestamp=float(i))

    rows = store.query_range("pod-17", "temperature", limit=10)

    # Bounded to the 10 *most recent* readings, but still returned oldest to
    # newest (Qodo 3581287838: a caller must never have to fetch all 50 rows
    # to get the last 10 -- the LIMIT has to happen in SQL, not a Python
    # slice after a full fetchall).
    assert [r.timestamp for r in rows] == [float(t) for t in range(40, 50)]


def test_query_range_limit_combines_with_since_and_until(store: Store) -> None:
    store.upsert_location("pod-18", kind="pod", product_model="airq")
    for ts in (100.0, 200.0, 300.0, 400.0, 500.0):
        store.record_reading("pod-18", "temperature", ts / 10, timestamp=ts)

    rows = store.query_range("pod-18", "temperature", since=150.0, until=450.0, limit=2)

    # In range [150, 450]: 200, 300, 400 -> most recent 2 -> 300, 400.
    assert [r.timestamp for r in rows] == [300.0, 400.0]


def test_query_range_without_limit_stays_unbounded(store: Store) -> None:
    store.upsert_location("pod-19", kind="pod", product_model="airq")
    for i in range(20):
        store.record_reading("pod-19", "temperature", float(i), timestamp=float(i))

    rows = store.query_range("pod-19", "temperature")
    assert len(rows) == 20


def test_build_range_query_applies_limit_sql_side_not_via_python_slicing() -> None:
    # White-box check on the query builder itself: when `limit` is given,
    # the LIMIT clause must be baked into the SQL text (and the limit value
    # bound as a parameter) -- proof this is SQL-side bounding, not a
    # fetchall-then-slice in Python.
    from sensibo.store.store import _build_range_query

    sql, params = _build_range_query("pod-x", "temperature", None, None, 25)
    assert "LIMIT ?" in sql
    assert params[-1] == 25

    sql, params = _build_range_query("pod-x", "temperature", 1.0, 2.0, 25)
    assert "LIMIT ?" in sql
    assert params == ("pod-x", "temperature", 1.0, 2.0, 25)

    # No limit given -> the plain unbounded query, no LIMIT clause at all.
    sql, params = _build_range_query("pod-x", "temperature", None, None, None)
    assert "LIMIT" not in sql
    assert params == ("pod-x", "temperature")


def test_latest_reading_picks_the_newest_timestamp(store: Store) -> None:
    store.upsert_location("pod-12", kind="pod", product_model="airq")
    store.record_reading("pod-12", "temperature", 19.0, timestamp=100.0)
    store.record_reading("pod-12", "temperature", 21.0, timestamp=200.0)

    reading = store.latest_reading("pod-12", "temperature")
    assert reading.value == 21.0
    assert reading.timestamp == 200.0


def test_latest_reading_for_unknown_field_is_none(store: Store) -> None:
    store.upsert_location("pod-13", kind="pod", product_model="airq")
    assert store.latest_reading("pod-13", "neverRecorded") is None


# --- retention: prune removes only rows older than the window ---------------


def test_default_retention_is_at_least_two_years() -> None:
    assert DEFAULT_RETENTION_DAYS >= 730


def test_prune_removes_only_rows_older_than_the_window(store: Store) -> None:
    store.upsert_location("pod-14", kind="pod", product_model="airq")
    now = 100_000_000.0
    old_ts = now - (800 * 86400)  # well past a 730-day window
    boundary_ts = now - (10 * 86400)  # comfortably inside the window
    store.record_reading("pod-14", "temperature", 1.0, timestamp=old_ts)
    store.record_reading("pod-14", "temperature", 2.0, timestamp=boundary_ts)

    deleted = store.prune(retention_days=730, now=now)

    assert deleted == 1
    remaining = store.query_range("pod-14", "temperature")
    assert [r.timestamp for r in remaining] == [boundary_ts]


def test_prune_never_touches_rows_newer_than_the_window(store: Store) -> None:
    store.upsert_location("pod-15", kind="pod", product_model="airq")
    now = 100_000_000.0
    recent_ts = now - 3600.0  # one hour old
    store.record_reading("pod-15", "temperature", 5.0, timestamp=recent_ts)

    deleted = store.prune(retention_days=730, now=now)

    assert deleted == 0
    remaining = store.query_range("pod-15", "temperature")
    assert len(remaining) == 1


def test_prune_retention_days_is_configurable(store: Store) -> None:
    store.upsert_location("pod-16", kind="pod", product_model="airq")
    now = 100_000_000.0
    ts = now - (10 * 86400)
    store.record_reading("pod-16", "temperature", 5.0, timestamp=ts)

    # A tight 5-day retention window prunes a 10-day-old row.
    deleted = store.prune(retention_days=5, now=now)
    assert deleted == 1


# --- offline: zero network code, proven by blocking the socket module -------


def test_store_write_and_query_survive_a_blocked_network(tmp_path: Path, monkeypatch) -> None:
    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise OSError("network disabled for this test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)

    offline_store = Store(db_path=tmp_path / "offline.db")
    try:
        offline_store.upsert_location("pod-off", kind="pod", product_model="pure")
        offline_store.record_reading("pod-off", "pm25", 1, timestamp=time.time())
        reading = offline_store.latest_reading("pod-off", "pm25")
        assert reading is not None
        assert reading.unit == "aqi"
    finally:
        offline_store.close()


def test_store_module_contains_no_network_symbols() -> None:
    # Belt-and-braces on top of the socket-blocking test above: the store
    # source itself must never reference socket/urllib/http.
    store_dir = Path(__file__).resolve().parent.parent / "sensibo" / "store"
    banned = {"socket", "urllib", "http", "requests", "aiohttp"}
    for path in store_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            else:
                continue
            assert not (names & banned), f"{path} imports network module: {names & banned}"


# --- db path resolution: default, parameter, and SENSIBO_DB env var --------


def test_default_db_path_is_under_dot_sensibo_in_home(monkeypatch) -> None:
    monkeypatch.delenv("SENSIBO_DB", raising=False)
    path = default_db_path()
    assert path == Path.home() / ".sensibo" / "sensibo.db"


def test_default_db_path_honors_sensibo_db_env_var(tmp_path: Path, monkeypatch) -> None:
    override = tmp_path / "custom-dir" / "custom.db"
    monkeypatch.setenv("SENSIBO_DB", str(override))
    assert default_db_path() == override


def test_store_explicit_param_wins_over_env_var(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SENSIBO_DB", str(tmp_path / "env-path.db"))
    explicit = tmp_path / "explicit.db"
    s = Store(db_path=explicit)
    try:
        assert explicit.exists()
        assert not (tmp_path / "env-path.db").exists()
    finally:
        s.close()


def test_store_creates_parent_directory_with_restrictive_permissions(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "sensibo.db"
    s = Store(db_path=db_path)
    try:
        assert db_path.exists()
        mode = db_path.parent.stat().st_mode & 0o777
        assert mode == 0o700
    finally:
        s.close()


def test_store_never_touches_the_real_home_directory(monkeypatch, tmp_path: Path) -> None:
    # Regression guard for the test suite itself: even if SENSIBO_DB and the
    # explicit param were both omitted, nothing in this file should have
    # created a real ~/.sensibo. We can't touch the real home from a test, so
    # this asserts the fixture-provided store always used tmp_path.
    fake_home = tmp_path / "fake-home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("SENSIBO_DB", raising=False)
    assert default_db_path() == fake_home / ".sensibo" / "sensibo.db"
    assert not fake_home.exists()  # resolving the path must not create it


# --- context manager convenience --------------------------------------------


def test_store_is_usable_as_a_context_manager(tmp_path: Path) -> None:
    with Store(db_path=tmp_path / "ctx.db") as s:
        s.upsert_location("pod-ctx", kind="pod", product_model="airq")
        s.record_reading("pod-ctx", "temperature", 22.0)
        assert s.latest_reading("pod-ctx", "temperature").value == 22.0


# --- ReadingRecord / LocationRecord shape ------------------------------------


def test_reading_record_has_expected_fields() -> None:
    r = ReadingRecord(
        location_id="pod-x",
        field="temperature",
        timestamp=1.0,
        value=20.0,
        unit="C",
    )
    assert r.location_id == "pod-x"
    assert r.field == "temperature"
    assert r.value == 20.0
    assert r.unit == "C"


# --- layering: pure storage, no CLI/API imports ------------------------------


def test_store_package_does_not_import_cli_or_api() -> None:
    store_dir = Path(__file__).resolve().parent.parent / "sensibo" / "store"
    for path in store_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("sensibo.cli"), path
                assert not node.module.startswith("sensibo.api"), path
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("sensibo.cli"), path
                    assert not alias.name.startswith("sensibo.api"), path


def test_store_package_is_stdlib_only() -> None:
    # Zero runtime dependencies is a hard rule (pyproject dependencies = []).
    # The store may only reach for stdlib modules.
    allowed_top_level = {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "json",
        "os",
        "pathlib",
        "sqlite3",
        "sys",
        "time",
        "types",
        "typing",
        "sensibo",
    }
    store_dir = Path(__file__).resolve().parent.parent / "sensibo" / "store"
    for path in store_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    continue  # intra-package relative import, e.g. `from ._paths import ...`
                if not node.module:
                    continue
                mods = {node.module.split(".")[0]}
            else:
                continue
            unexpected = mods - allowed_top_level
            assert not unexpected, f"{path} imports non-stdlib module(s): {unexpected}"


# --- deviation d1: batched writes ------------------------------------------


def test_record_readings_is_one_transaction_and_record_series_bulk_inserts(tmp_path):
    import time as _time

    store = Store(db_path=tmp_path / "bulk.db")
    store.upsert_location("pod-1", kind="pod", product_model="airq")
    # record_readings: one commit per call, every field lands.
    store.record_readings("pod-1", {"temperature": 21.5, "humidity": 44}, timestamp=1_700_000_000)
    latest = store.latest_readings("pod-1")
    assert {latest["temperature"].value, latest["humidity"].value} == {21.5, 44.0}
    # record_series: 6720 points (7 days at 90s) in well under a second.
    points = [(1_700_000_000 + i * 90, 20.0 + (i % 7)) for i in range(6720)]
    started = _time.perf_counter()
    written = store.record_series("pod-1", "co2", points)
    elapsed = _time.perf_counter() - started
    assert written == 6720
    assert elapsed < 2.0, f"record_series took {elapsed:.2f}s"
    rows = store.query_range("pod-1", "co2")
    assert len(rows) == 6720
    assert rows[0].unit == "ppm"
    # idempotent: re-recording the same points upserts, never duplicates.
    assert store.record_series("pod-1", "co2", points) == 6720
    assert len(store.query_range("pod-1", "co2")) == 6720
    assert store.record_series("pod-1", "co2", []) == 0
    store.close()
