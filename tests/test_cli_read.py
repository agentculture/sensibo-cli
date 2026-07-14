"""Tests for ``sensibo read <pod-or-location-id>`` (task t4).

Same hard rules as test_cli_devices.py: mock the network at the client seam,
never touch the real ``~/.sensibo`` or a real key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sensibo.api.client as client_module
from sensibo.cli import main
from tests._fixtures_fleet import AIRQ_POD, FLEET_PAYLOAD, POD_ID, FakeUrlopen


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))


def _with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENSIBO_API_KEY", "TESTKEY")


# --- reading a pod: every field, plus nested motionSensors -------------------


def test_read_pod_prints_every_reading_including_motion_sensors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_key(monkeypatch)
    fake = FakeUrlopen(FLEET_PAYLOAD)
    monkeypatch.setattr(client_module, "urlopen", fake)

    rc = main(["read", POD_ID, "--json"])

    assert rc == 0
    assert len(fake.calls) == 1  # built on the same single fleet_snapshot() call
    payload = json.loads(capsys.readouterr().out)

    assert payload["id"] == POD_ID
    assert payload["productModel"] == "airq"
    assert payload["room"] == "Living Room"
    assert payload["connectionStatus"] == "online"
    assert payload["readings"] == AIRQ_POD["measurements"]
    for field in (
        "temperature",
        "humidity",
        "feelsLike",
        "motion",
        "roomIsOccupied",
        "tvoc",
        "co2",
        "iaq",
        "rssi",
    ):
        assert field in payload["readings"]

    sensors = payload["motionSensors"]
    assert [s["id"] for s in sensors] == ["ms_aaa111", "ms_bbb222"]
    live, stale = sensors
    assert live["productModel"] == "motion_sensor"
    assert live["parentDeviceUid"] == POD_ID
    assert live["readings"] == {
        "temperature": 23.9,
        "humidity": 39,
        "motion": False,
        "battery": 87,
        "rssi": -60,
    }
    assert stale["readings"] == {}


# --- reading a Room Sensor directly by its ms_* id ---------------------------


def test_read_room_sensor_by_id_returns_its_own_readings_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_key(monkeypatch)
    monkeypatch.setattr(client_module, "urlopen", FakeUrlopen(FLEET_PAYLOAD))

    rc = main(["read", "ms_aaa111", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["id"] == "ms_aaa111"
    assert payload["parentDeviceUid"] == POD_ID
    assert payload["readings"]["temperature"] == 23.9
    assert "motionSensors" not in payload  # a Room Sensor has none of its own
    assert "room" not in payload


# --- unknown id: structured error, not a crash -------------------------------


def test_read_unknown_id_errors_with_hint_pointing_at_devices(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_key(monkeypatch)
    monkeypatch.setattr(client_module, "urlopen", FakeUrlopen(FLEET_PAYLOAD))

    rc = main(["read", "no-such-id"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "sensibo devices" in err


# --- missing positional argument: structured argparse error ------------------


def test_read_missing_location_id_is_a_structured_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["read"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- text output --------------------------------------------------------------


def test_read_text_output_prints_readings_and_nested_sensors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_key(monkeypatch)
    monkeypatch.setattr(client_module, "urlopen", FakeUrlopen(FLEET_PAYLOAD))

    rc = main(["read", POD_ID])
    assert rc == 0
    out = capsys.readouterr().out
    assert POD_ID in out
    assert "Living Room" in out
    assert "temperature: 24.5" in out
    assert "ms_aaa111" in out
    assert "battery: 87" in out


def test_read_text_output_for_room_sensor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_key(monkeypatch)
    monkeypatch.setattr(client_module, "urlopen", FakeUrlopen(FLEET_PAYLOAD))

    rc = main(["read", "ms_aaa111"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ms_aaa111" in out
    assert f"parent {POD_ID}" in out
    assert "battery: 87" in out
