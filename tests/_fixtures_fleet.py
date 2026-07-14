"""Shared realistic fleet fixtures for the ``devices``/``read`` verb tests (task t4).

The ``airq`` pod mirrors the field set ``docs/sensibo-api.md`` records as
CONFIRMED against the operator's real Air Pro pod: temperature, humidity,
feelsLike, motion, roomIsOccupied, tvoc, co2, iaq, rssi — plus two nested
Room Sensors (``motionSensors[]``, stable ``ms_*`` ids under
``parentDeviceUid``, own ``measurements``) matching the real-fleet finding in
``docs/specs/2026-07-14-sensibo-cli-ships-the-full-product-one-cli-and-age.md``:
one live, one stale (empty current measurements — "likely offline or
battery-dead").
"""

from __future__ import annotations

import json

POD_ID = "pod-airq-1"

LIVE_ROOM_SENSOR = {
    "id": "ms_aaa111",
    "productModel": "motion_sensor",
    "parentDeviceUid": POD_ID,
    "connectionStatus": {"isAlive": True},
    "measurements": {
        "temperature": 23.9,
        "humidity": 39,
        "motion": False,
        "battery": 87,
        "rssi": -60,
    },
}

STALE_ROOM_SENSOR = {
    "id": "ms_bbb222",
    "productModel": "motion_sensor",
    "parentDeviceUid": POD_ID,
    "connectionStatus": {"isAlive": False},
    "measurements": {},
}

AIRQ_POD = {
    "id": POD_ID,
    "productModel": "airq",
    "room": {"name": "Living Room", "uid": "room-1"},
    "connectionStatus": {"isAlive": True},
    "measurements": {
        "temperature": 24.5,
        "humidity": 41,
        "feelsLike": 25.0,
        "motion": True,
        "roomIsOccupied": True,
        "tvoc": 120,
        "co2": 650,
        "iaq": 1,
        "rssi": -45,
    },
    "motionSensors": [LIVE_ROOM_SENSOR, STALE_ROOM_SENSOR],
}

FLEET_PAYLOAD = {"result": [AIRQ_POD]}


class FakeHeaders:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self._data = {k.lower(): v for k, v in (data or {}).items()}

    def get(self, name: str, default: str = "") -> str:
        return self._data.get(name.lower(), default)


class FakeResponse:
    """Minimal stand-in for the object ``urlopen()`` returns."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = FakeHeaders(headers)

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass


class FakeUrlopen:
    """Always returns the same canned JSON body; records every request made.

    Patched in for ``sensibo.api.client.urlopen`` — the one seam
    ``SensiboClient`` calls through — so tests never touch the real network.
    """

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.calls: list = []

    def __call__(self, req, timeout=None):  # noqa: ANN001 - test double, matches urlopen sig
        self.calls.append(req)
        return FakeResponse(self._body)
