"""Tests for ``sensibo devices`` (task t4).

Hard rules from the task brief: mock all network at the client seam (never a
real HTTP call), never touch the real ``~/.sensibo`` or a real key. Every test
here redirects ``HOME`` to a fresh ``tmp_path`` before anything else runs, so
even a test that forgets to set ``SENSIBO_API_KEY`` can never fall through to
a real operator dotenv file.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError

import pytest

import sensibo.api.client as client_module
from sensibo.cli import main
from tests._fixtures_fleet import AIRQ_POD, FLEET_PAYLOAD, POD_ID, FakeUrlopen


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No real ~/.sensibo/.env can ever be consulted from these tests.
    monkeypatch.setenv("HOME", str(tmp_path))


def _with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENSIBO_API_KEY", "TESTKEY")


# --- exactly one HTTP call ---------------------------------------------------


def test_devices_performs_exactly_one_http_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_key(monkeypatch)
    fake = FakeUrlopen(FLEET_PAYLOAD)
    monkeypatch.setattr(client_module, "urlopen", fake)

    rc = main(["devices", "--json"])

    assert rc == 0
    assert len(fake.calls) == 1
    req = fake.calls[0]
    assert req.get_method() == "GET"
    assert "/users/me/pods" in req.full_url
    capsys.readouterr()


# --- JSON shape ---------------------------------------------------------------


def test_devices_json_lists_pod_with_model_room_status_and_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_key(monkeypatch)
    monkeypatch.setattr(client_module, "urlopen", FakeUrlopen(FLEET_PAYLOAD))

    rc = main(["devices", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert "asOf" in payload and payload["asOf"]
    [pod] = payload["devices"]
    assert pod["id"] == POD_ID
    assert pod["productModel"] == "airq"
    assert pod["room"] == "Living Room"
    assert pod["connectionStatus"] == "online"
    assert pod["fields"] == sorted(AIRQ_POD["measurements"])


def test_devices_json_lists_room_sensors_nested_under_their_parent_pod(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_key(monkeypatch)
    monkeypatch.setattr(client_module, "urlopen", FakeUrlopen(FLEET_PAYLOAD))

    rc = main(["devices", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    [pod] = payload["devices"]
    sensors = pod["roomSensors"]
    assert [s["id"] for s in sensors] == ["ms_aaa111", "ms_bbb222"]

    live, stale = sensors
    assert live["productModel"] == "motion_sensor"
    assert live["parentDeviceUid"] == POD_ID
    assert live["connectionStatus"] == "online"
    assert set(live["fields"]) == {"temperature", "humidity", "motion", "battery", "rssi"}
    assert live["lastSeen"] == payload["asOf"]

    # the stale sensor reported nothing this snapshot: no fields, no last-seen
    assert stale["connectionStatus"] == "offline"
    assert stale["fields"] == []
    assert stale["lastSeen"] is None


def test_devices_never_lists_room_sensors_as_top_level_devices(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_key(monkeypatch)
    monkeypatch.setattr(client_module, "urlopen", FakeUrlopen(FLEET_PAYLOAD))

    rc = main(["devices", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert len(payload["devices"]) == 1
    assert all("ms_" not in d["id"] for d in payload["devices"])


# --- text output --------------------------------------------------------------


def test_devices_text_output_is_human_readable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_key(monkeypatch)
    monkeypatch.setattr(client_module, "urlopen", FakeUrlopen(FLEET_PAYLOAD))

    rc = main(["devices"])
    assert rc == 0
    out = capsys.readouterr().out
    assert POD_ID in out
    assert "airq" in out
    assert "Living Room" in out
    assert "online" in out
    assert "ms_aaa111" in out
    assert "temperature" in out


def test_devices_text_output_notes_empty_fleet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_key(monkeypatch)
    monkeypatch.setattr(client_module, "urlopen", FakeUrlopen({"result": []}))

    rc = main(["devices"])
    assert rc == 0
    assert "no pods found" in capsys.readouterr().out


# --- error mapping: ApiError -> CliError -------------------------------------


def test_devices_missing_api_key_maps_to_cli_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A CliError raised *during* a command's execution is caught by
    # `_dispatch` and returned as an int exit code - it does not raise
    # SystemExit (unlike an argparse-level parse error).
    rc = main(["devices"])
    assert rc == 2  # ERROR_AUTH
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


def test_devices_network_error_maps_to_cli_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_key(monkeypatch)

    def _raise(req, timeout=None):  # noqa: ANN001 - test double, matches urlopen sig
        raise URLError("Name or service not known")

    monkeypatch.setattr(client_module, "urlopen", _raise)

    rc = main(["devices"])
    assert rc == 2  # network failure is an environment error (exit codes: 0/1/2, 3+ reserved)
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- naming: devices names the installed command in --help -----------------


def test_devices_help_names_the_installed_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["devices", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: sensibo devices" in out
