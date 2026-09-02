"""Tests for ``sensibo.mcp_server._tools`` — the MCP tool implementations (task t11).

These exercise the tool *functions* directly — no MCP wire format involved —
mirroring ``tests/test_cli_set.py``'s approach for the CLI's own ``set``
verb: mock the API client at the class seam
(``sensibo.mcp_server._tools.SensiboClient``), and never touch the real
network or the real ``~/.sensibo``. ``set_ac_state``'s dry-run-by-default
safety contract is the load-bearing property under test: ``apply=False`` (the
default) must issue **zero** write calls, exactly like ``sensibo set``
without ``--apply``.
"""

from __future__ import annotations

import datetime
import time
from pathlib import Path
from typing import Any

import pytest

import sensibo.mcp_server._tools as tools
from sensibo.api import HttpError
from sensibo.health.model import STATUS_DOWN, STATUS_OK
from sensibo.store import KIND_POD, KIND_ROOM_SENSOR, Store

POD_ID = "pod-airq-1"

LIVE_ROOM_SENSOR = {
    "id": "ms_aaa111",
    "productModel": "motion_sensor",
    "parentDeviceUid": POD_ID,
    "connectionStatus": {"isAlive": True},
    "measurements": {"temperature": 23.9, "humidity": 39, "battery": 87},
}

AIRQ_POD = {
    "id": POD_ID,
    "productModel": "airq",
    "room": {"name": "Living Room", "uid": "room-1"},
    "connectionStatus": {"isAlive": True},
    "measurements": {"temperature": 24.5, "humidity": 41, "co2": 650},
    "motionSensors": [LIVE_ROOM_SENSOR],
}

FLEET_PAYLOAD = {"result": [AIRQ_POD]}


class _FakeFleetClient:
    """Just enough of ``SensiboClient`` for list_devices/read_location tests."""

    def __init__(self, payload: object = FLEET_PAYLOAD) -> None:
        self._payload = payload
        self.calls: list[str] = []

    def fleet_snapshot(self, fields: str = "*") -> object:
        self.calls.append("fleet_snapshot")
        return self._payload


class _FakeAcStateClient:
    """Records every call; simulates acStates reads/writes in-memory (mirrors
    ``tests/test_cli_set.py``'s ``_FakeClient``)."""

    def __init__(self, pods: dict[str, dict[str, Any]]) -> None:
        self._pods = {pod_id: dict(state) for pod_id, state in pods.items()}
        self.calls: list[tuple[str, tuple, dict]] = []

    def get_pod(self, pod_id: str, fields: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_pod", (pod_id,), {"fields": fields}))
        return {"result": {"acState": dict(self._pods[pod_id])}}

    def patch_ac_state(
        self, pod_id: str, prop: str, current_ac_state: dict[str, Any], new_value: object
    ) -> dict[str, Any]:
        self.calls.append(("patch_ac_state", (pod_id, prop, dict(current_ac_state), new_value), {}))
        self._pods[pod_id][prop] = new_value
        return {"result": dict(self._pods[pod_id])}

    def post_ac_states(self, pod_id: str, ac_state: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("post_ac_states", (pod_id, dict(ac_state)), {}))
        self._pods[pod_id].update(ac_state)
        return {"result": dict(self._pods[pod_id])}


class _RaisingClient:
    """Every method blows up if called — proves a dry run makes zero write calls."""

    def get_pod(self, pod_id: str, fields: str | None = None) -> dict[str, Any]:
        raise AssertionError("get_pod should not be called in this test")

    def patch_ac_state(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("patch_ac_state must never be called on a dry run")

    def post_ac_states(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("post_ac_states must never be called on a dry run")


def _install(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    monkeypatch.setattr(tools, "SensiboClient", lambda *a, **kw: client)


# --- list_devices ------------------------------------------------------------


def test_list_devices_shapes_the_fleet_from_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeFleetClient()
    _install(monkeypatch, fake)

    result = tools.list_devices()

    assert fake.calls == ["fleet_snapshot"]  # exactly one call, never per-device
    assert "as_of" in result
    (device,) = result["devices"]
    assert device["id"] == POD_ID
    assert device["product_model"] == "airq"
    assert device["room"] == "Living Room"
    assert device["connection_status"] == "online"
    assert set(device["fields"]) == {"temperature", "humidity", "co2"}
    (sensor,) = device["room_sensors"]
    assert sensor["id"] == "ms_aaa111"
    assert sensor["parent_pod_id"] == POD_ID


def test_list_devices_maps_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenClient:
        def fleet_snapshot(self, fields: str = "*") -> None:
            raise HttpError(message="HTTP 500", status=500, remediation="try later")

    _install(monkeypatch, _BrokenClient())

    with pytest.raises(RuntimeError, match="HTTP 500"):
        tools.list_devices()


# --- read_location -------------------------------------------------------------


def test_read_location_by_pod_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install(monkeypatch, _FakeFleetClient())

    result = tools.read_location(POD_ID, db=str(tmp_path / "sensibo.db"))

    assert result["id"] == POD_ID
    assert result["readings"] == AIRQ_POD["measurements"]
    (sensor,) = result["room_sensors"]
    assert sensor["id"] == "ms_aaa111"


def test_read_location_by_room_sensor_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install(monkeypatch, _FakeFleetClient())

    result = tools.read_location("ms_aaa111", db=str(tmp_path / "sensibo.db"))

    assert result["id"] == "ms_aaa111"
    assert result["parent_pod_id"] == POD_ID
    assert result["readings"] == LIVE_ROOM_SENSOR["measurements"]


def test_read_location_resolves_a_store_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "sensibo.db"
    with Store(db_path=db_path) as store:
        store.upsert_location(POD_ID, kind=KIND_POD, product_model="airq")
        store.set_alias(POD_ID, "Living Room AC")

    _install(monkeypatch, _FakeFleetClient())

    result = tools.read_location("Living Room AC", db=str(db_path))

    assert result["id"] == POD_ID


def test_read_location_unknown_id_raises_lookup_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, _FakeFleetClient())
    db = str(tmp_path / "sensibo.db")

    with pytest.raises(LookupError, match="no-such-id"):
        tools.read_location("no-such-id", db=db)


def test_read_location_maps_api_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class _BrokenClient:
        def fleet_snapshot(self, fields: str = "*") -> None:
            raise HttpError(message="HTTP 500", status=500, remediation="try later")

    _install(monkeypatch, _BrokenClient())
    db = str(tmp_path / "sensibo.db")

    with pytest.raises(RuntimeError, match="HTTP 500"):
        tools.read_location(POD_ID, db=db)


# --- query_history: local store only, no client needed ----------------------


def _seed_history(db_path: Path) -> None:
    with Store(db_path=db_path) as store:
        store.upsert_location(POD_ID, kind=KIND_POD, product_model="airq", room_name="Office")
        store.record_reading(POD_ID, "temperature", 20.0, timestamp=100.0)
        store.record_reading(POD_ID, "temperature", 21.0, timestamp=200.0)
        store.record_reading(POD_ID, "humidity", 55.0, timestamp=200.0)


def test_query_history_latest_all_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "sensibo.db"
    _seed_history(db_path)

    result = tools.query_history(POD_ID, db=str(db_path))

    assert result["mode"] == "latest"
    values = {r["field"]: r["value"] for r in result["readings"]}
    assert values == {"temperature": 21.0, "humidity": 55.0}


def test_query_history_latest_one_field(tmp_path: Path) -> None:
    db_path = tmp_path / "sensibo.db"
    _seed_history(db_path)

    result = tools.query_history(POD_ID, field="temperature", db=str(db_path))

    (reading,) = result["readings"]
    assert reading["value"] == 21.0


def test_query_history_range_is_inclusive(tmp_path: Path) -> None:
    db_path = tmp_path / "sensibo.db"
    _seed_history(db_path)

    result = tools.query_history(
        POD_ID,
        field="temperature",
        mode="range",
        since="1970-01-01T00:01:40Z",  # 100.0s
        until="1970-01-01T00:03:20Z",  # 200.0s
        db=str(db_path),
    )

    values = [r["value"] for r in result["readings"]]
    assert values == [20.0, 21.0]


def test_query_history_range_requires_field(tmp_path: Path) -> None:
    db_path = tmp_path / "sensibo.db"
    _seed_history(db_path)
    db = str(db_path)

    with pytest.raises(ValueError, match="field"):
        tools.query_history(POD_ID, mode="range", db=db)


def test_query_history_resolves_by_alias(tmp_path: Path) -> None:
    db_path = tmp_path / "sensibo.db"
    _seed_history(db_path)
    with Store(db_path=db_path) as store:
        store.set_alias(POD_ID, "Office AC")

    result = tools.query_history("Office AC", field="temperature", db=str(db_path))

    assert result["location_id"] == POD_ID


def test_query_history_unknown_location_raises_lookup_error(tmp_path: Path) -> None:
    db_path = tmp_path / "sensibo.db"
    _seed_history(db_path)
    db = str(db_path)

    with pytest.raises(LookupError):
        tools.query_history("no-such-location", db=db)


def test_query_history_invalid_mode_raises_value_error(tmp_path: Path) -> None:
    db_path = tmp_path / "sensibo.db"
    _seed_history(db_path)
    db = str(db_path)

    with pytest.raises(ValueError, match="mode"):
        tools.query_history(POD_ID, mode="bogus", db=db)


def test_query_history_invalid_timestamp_raises_value_error(tmp_path: Path) -> None:
    db_path = tmp_path / "sensibo.db"
    _seed_history(db_path)
    db = str(db_path)

    with pytest.raises(ValueError, match="since"):
        tools.query_history(
            POD_ID, field="temperature", mode="range", since="not-a-timestamp", db=db
        )


# --- set_ac_state: apply defaults to False, zero writes on a dry run --------


def test_set_ac_state_dry_run_by_default_makes_zero_write_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAcStateClient({"pod1": {"on": False, "mode": "heat", "targetTemperature": 20}})
    _install(monkeypatch, fake)

    result = tools.set_ac_state("pod1", mode="cool", target=22)

    assert result["apply"] is False
    assert [c[0] for c in fake.calls] == ["get_pod"]  # exactly one read, zero writes
    assert result["changes"] == {
        "mode": {"from": "heat", "to": "cool"},
        "targetTemperature": {"from": 20, "to": 22},
    }
    assert "result_ac_state" not in result


def test_set_ac_state_dry_run_never_calls_patch_or_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ReadOnlyClient(_RaisingClient):
        def get_pod(self, pod_id: str, fields: str | None = None) -> dict[str, Any]:
            return {"result": {"acState": {"on": True, "mode": "heat"}}}

    _install(monkeypatch, _ReadOnlyClient())

    result = tools.set_ac_state("pod1", power="off")  # apply defaults to False

    assert result["apply"] is False  # would not raise AssertionError from patch/post


def test_set_ac_state_apply_true_single_field_uses_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAcStateClient({"pod1": {"on": True, "mode": "heat", "targetTemperature": 20}})
    _install(monkeypatch, fake)

    result = tools.set_ac_state("pod1", mode="cool", apply=True)

    assert result["apply"] is True
    assert result["method"] == "patch"
    kinds = [c[0] for c in fake.calls]
    assert kinds.count("patch_ac_state") == 1
    assert "post_ac_states" not in kinds
    assert fake._pods["pod1"]["mode"] == "cool"
    assert result["result_ac_state"]["mode"] == "cool"


def test_set_ac_state_apply_true_multi_field_uses_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAcStateClient({"pod1": {"on": False, "mode": "heat", "targetTemperature": 20}})
    _install(monkeypatch, fake)

    result = tools.set_ac_state("pod1", power="on", mode="cool", target=22, apply=True)

    assert result["method"] == "post"
    kinds = [c[0] for c in fake.calls]
    assert kinds.count("post_ac_states") == 1
    assert "patch_ac_state" not in kinds
    assert fake._pods["pod1"] == {"on": True, "mode": "cool", "targetTemperature": 22}


def test_set_ac_state_apply_true_with_no_actual_changes_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAcStateClient({"pod1": {"on": True, "mode": "cool"}})
    _install(monkeypatch, fake)

    result = tools.set_ac_state("pod1", mode="cool", apply=True)

    assert result["changes"] == {}
    kinds = [c[0] for c in fake.calls]
    assert "patch_ac_state" not in kinds
    assert "post_ac_states" not in kinds


def test_set_ac_state_no_fields_given_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _RaisingClient())

    with pytest.raises(ValueError, match="no fields"):
        tools.set_ac_state("pod1")


def test_set_ac_state_invalid_power_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _RaisingClient())

    with pytest.raises(ValueError, match="power"):
        tools.set_ac_state("pod1", power="sideways")


def test_set_ac_state_missing_ac_state_raises_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoAcStateClient:
        def get_pod(self, pod_id: str, fields: str | None = None) -> dict[str, Any]:
            return {"result": {}}

    _install(monkeypatch, _NoAcStateClient())

    with pytest.raises(LookupError, match="acState"):
        tools.set_ac_state("pod1", mode="cool")


def test_set_ac_state_maps_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenClient:
        def get_pod(self, pod_id: str, fields: str | None = None) -> None:
            raise HttpError(message="HTTP 404 no such pod", status=404, remediation="check id")

    _install(monkeypatch, _BrokenClient())

    with pytest.raises(RuntimeError, match="HTTP 404"):
        tools.set_ac_state("no-such-pod", mode="cool")


def test_set_ac_state_maps_api_error_during_apply_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenOnWriteClient:
        def get_pod(self, pod_id: str, fields: str | None = None) -> dict[str, Any]:
            return {"result": {"acState": {"mode": "heat"}}}

        def patch_ac_state(self, *args: object, **kwargs: object) -> None:
            raise HttpError(message="HTTP 429", status=429, remediation="back off")

    _install(monkeypatch, _BrokenOnWriteClient())

    with pytest.raises(RuntimeError, match="HTTP 429"):
        tools.set_ac_state("pod1", mode="cool", apply=True)


def test_set_ac_state_fan_and_swing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAcStateClient({"pod1": {"fanLevel": "low", "swing": "fixedTop"}})
    _install(monkeypatch, fake)

    result = tools.set_ac_state("pod1", fan="high", swing="rangeFull")

    assert result["changes"] == {
        "fanLevel": {"from": "low", "to": "high"},
        "swing": {"from": "fixedTop", "to": "rangeFull"},
    }


# --- room_list ---------------------------------------------------------------


def test_room_list_reports_alias_and_staleness(tmp_path: Path) -> None:
    db_path = tmp_path / "sensibo.db"
    now = time.time()
    long_ago = now - (48 * 3600)
    with Store(db_path=db_path) as store:
        store.upsert_location(
            POD_ID, kind=KIND_POD, product_model="airq", room_name="Living Room", seen_at=now
        )
        store.set_alias(POD_ID, "Main AC")
        store.upsert_location(
            "ms_stale",
            kind=KIND_ROOM_SENSOR,
            parent_pod_id=POD_ID,
            room_name="Bedroom",
            seen_at=long_ago,
        )

    result = tools.room_list(db=str(db_path))

    by_id = {loc["id"]: loc for loc in result["locations"]}
    assert by_id[POD_ID]["alias"] == "Main AC"
    assert by_id[POD_ID]["stale"] is False
    assert by_id["ms_stale"]["stale"] is True


def test_room_list_empty_store(tmp_path: Path) -> None:
    result = tools.room_list(db=str(tmp_path / "sensibo.db"))
    assert result["locations"] == []


def test_room_list_default_stale_after_hours_derives_from_health_config_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENSIBO_HEALTH_DOWN_AFTER", "1800")
    result = tools.room_list(db=str(tmp_path / "sensibo.db"))
    assert result["stale_after_hours"] == pytest.approx(1800.0 / 3600.0)


def test_room_list_carries_health_fields_when_a_row_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "sensibo.db"
    now = time.time()
    with Store(db_path=db_path) as store:
        store.upsert_location(POD_ID, kind=KIND_POD, product_model="airq", seen_at=now)
        store.upsert_location("pod-no-health", kind=KIND_POD, product_model="airq", seen_at=now)
        store.set_health(POD_ID, status=STATUS_DOWN, since=now - 500, last_ok=now - 900)

    result = tools.room_list(db=str(db_path))
    by_id = {loc["id"]: loc for loc in result["locations"]}
    assert by_id[POD_ID]["health_status"] == STATUS_DOWN
    assert by_id[POD_ID]["health_since"] == pytest.approx(now - 500)
    assert by_id[POD_ID]["health_last_ok"] == pytest.approx(now - 900)
    assert by_id["pod-no-health"]["health_status"] is None
    assert by_id["pod-no-health"]["stale"] is False  # falls back to the derived flag


# --- sensibo_health (task t9) -------------------------------------------------


def test_sensibo_health_reports_every_locations_health_row(tmp_path: Path) -> None:
    db_path = tmp_path / "sensibo.db"
    now = time.time()
    with Store(db_path=db_path) as store:
        store.upsert_location(POD_ID, kind=KIND_POD, product_model="airq", seen_at=now)
        store.set_health(POD_ID, status=STATUS_OK, since=now - 10, last_ok=now)
        store.set_health("ms_1", status=STATUS_DOWN, since=now - 500, last_ok=now - 900)

    result = tools.sensibo_health(db=str(db_path))

    by_id = {row["location_id"]: row for row in result["locations"]}
    assert by_id[POD_ID]["status"] == STATUS_OK
    assert by_id[POD_ID]["last_ok"] == pytest.approx(now)
    assert by_id["ms_1"]["status"] == STATUS_DOWN
    assert by_id["ms_1"]["since"] == pytest.approx(now - 500)


def test_sensibo_health_reports_the_collector_heartbeat(tmp_path: Path) -> None:
    db_path = tmp_path / "sensibo.db"
    with Store(db_path=db_path) as store:
        store.set_meta("last_cycle_at", "2026-09-02T12:00:00Z")
        store.set_meta("last_cycle_outcome", "ok")

    result = tools.sensibo_health(db=str(db_path))
    assert result["last_cycle_at"] == "2026-09-02T12:00:00Z"
    assert result["last_cycle_outcome"] == "ok"


def test_sensibo_health_heartbeat_is_none_when_no_cycle_has_run(tmp_path: Path) -> None:
    result = tools.sensibo_health(db=str(tmp_path / "sensibo.db"))
    assert result["last_cycle_at"] is None
    assert result["last_cycle_outcome"] is None


def test_sensibo_health_filters_transitions_by_since(tmp_path: Path) -> None:
    db_path = tmp_path / "sensibo.db"
    now = time.time()
    with Store(db_path=db_path) as store:
        store.record_transition(POD_ID, None, STATUS_OK, at=now - 1000)
        store.record_transition(POD_ID, STATUS_OK, STATUS_DOWN, at=now - 500)
        store.record_transition(POD_ID, STATUS_DOWN, STATUS_OK, at=now - 100)

    all_transitions = tools.sensibo_health(db=str(db_path))["transitions"]
    assert len(all_transitions) == 3

    since_iso = datetime.datetime.fromtimestamp(now - 600, tz=datetime.timezone.utc).isoformat()
    recent = tools.sensibo_health(since=since_iso, db=str(db_path))["transitions"]
    assert len(recent) == 2  # the down and the recovery, not the first-run seed


def test_sensibo_health_attaches_duration_seconds_to_closed_outages_only(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sensibo.db"
    now = time.time()
    with Store(db_path=db_path) as store:
        store.record_transition(POD_ID, None, STATUS_OK, at=now - 1000)
        store.record_transition(POD_ID, STATUS_OK, STATUS_DOWN, at=now - 500)
        store.record_transition(POD_ID, STATUS_DOWN, STATUS_OK, at=now - 100)
        # A second location's outage that never recovers -- stays open.
        store.record_transition("ms_1", None, STATUS_OK, at=now - 1000)
        store.record_transition("ms_1", STATUS_OK, STATUS_DOWN, at=now - 200)

    transitions = tools.sensibo_health(db=str(db_path))["transitions"]
    by_pair = {(t["location_id"], t["to_status"], t["at"]): t for t in transitions}

    closed = by_pair[(POD_ID, STATUS_OK, now - 100)]
    assert closed["duration_seconds"] == pytest.approx(400.0)

    still_down = by_pair[("ms_1", STATUS_DOWN, now - 200)]
    assert still_down["duration_seconds"] is None

    first_run_seed = by_pair[(POD_ID, STATUS_OK, now - 1000)]
    assert first_run_seed["duration_seconds"] is None
