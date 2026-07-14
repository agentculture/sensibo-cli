"""Unit tests for sensibo.cli._commands._fleet — pure data shaping, no network.

These exercise the fleet_snapshot()-payload -> Location shaping directly,
independent of the CLI/HTTP layer covered by test_cli_devices.py and
test_cli_read.py.
"""

from __future__ import annotations

from sensibo.cli._commands._fleet import (
    KIND_POD,
    KIND_ROOM_SENSOR,
    describe_fleet,
    find_location,
    pods_from_payload,
)
from tests._fixtures_fleet import AIRQ_POD, FLEET_PAYLOAD, POD_ID

AS_OF = "2026-07-14T12:00:00+00:00"


def test_pods_from_payload_extracts_result_list() -> None:
    assert pods_from_payload(FLEET_PAYLOAD) == [AIRQ_POD]


def test_pods_from_payload_defensive_on_malformed_shapes() -> None:
    assert pods_from_payload(None) == []
    assert pods_from_payload({}) == []
    assert pods_from_payload({"result": "not-a-list"}) == []
    assert pods_from_payload({"result": [1, "x", {"id": "ok"}]}) == [{"id": "ok"}]


def test_describe_fleet_shapes_the_pod() -> None:
    [pod] = describe_fleet(FLEET_PAYLOAD, AS_OF)
    assert pod.id == POD_ID
    assert pod.kind == KIND_POD
    assert pod.product_model == "airq"
    assert pod.room == "Living Room"
    assert pod.connection_status == "online"
    assert pod.fields == sorted(AIRQ_POD["measurements"])
    assert pod.readings == AIRQ_POD["measurements"]
    assert pod.last_seen == AS_OF


def test_describe_fleet_never_lists_room_sensors_as_top_level_locations() -> None:
    locations = describe_fleet(FLEET_PAYLOAD, AS_OF)
    assert len(locations) == 1  # the two Room Sensors are nested, not top-level


def test_describe_fleet_nests_room_sensors_under_their_parent() -> None:
    [pod] = describe_fleet(FLEET_PAYLOAD, AS_OF)
    assert [s.id for s in pod.room_sensors] == ["ms_aaa111", "ms_bbb222"]
    live, stale = pod.room_sensors
    assert live.kind == KIND_ROOM_SENSOR
    assert live.product_model == "motion_sensor"
    assert live.parent_pod_id == POD_ID
    assert live.connection_status == "online"
    assert live.fields == sorted(["temperature", "humidity", "motion", "battery", "rssi"])
    assert live.last_seen == AS_OF  # reported at least one field this snapshot


def test_room_sensor_with_no_current_measurements_has_no_last_seen() -> None:
    [pod] = describe_fleet(FLEET_PAYLOAD, AS_OF)
    _live, stale = pod.room_sensors
    assert stale.connection_status == "offline"
    assert stale.readings == {}
    assert stale.fields == []
    assert stale.last_seen is None


def test_find_location_resolves_a_pod_id() -> None:
    loc = find_location(FLEET_PAYLOAD, POD_ID, AS_OF)
    assert loc is not None
    assert loc.kind == KIND_POD
    assert loc.id == POD_ID


def test_find_location_resolves_a_nested_room_sensor_id() -> None:
    loc = find_location(FLEET_PAYLOAD, "ms_aaa111", AS_OF)
    assert loc is not None
    assert loc.kind == KIND_ROOM_SENSOR
    assert loc.parent_pod_id == POD_ID


def test_find_location_returns_none_for_unknown_id() -> None:
    assert find_location(FLEET_PAYLOAD, "bogus", AS_OF) is None


def test_describe_fleet_handles_pod_with_no_motion_sensors_key() -> None:
    payload = {"result": [{"id": "sky-1", "productModel": "skyv2", "measurements": {}}]}
    [pod] = describe_fleet(payload, AS_OF)
    assert pod.room_sensors == ()
    assert pod.room is None
    assert pod.last_seen is None
