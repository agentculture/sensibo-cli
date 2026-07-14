"""Tests for sensibo.web.WebServer — the LAN dashboard (task t12).

Written first (TDD): these fail against an empty ``sensibo/web`` package and
pass once ``sensibo/web/server.py`` lands. Every test spins a real
:class:`~sensibo.web.WebServer` on ``127.0.0.1:0`` (an ephemeral port)
against a ``tmp_path`` sqlite store, seeded directly through the ``Store``
API — never the real ``~/.sensibo``.

``loopback_only`` proves the read/browse paths need zero cloud: it blocks any
outbound ``socket.connect`` whose target host isn't loopback, so if a page
handler ever reached for the real Sensibo API by mistake, the test would
fail loudly (a connection attempt to a non-loopback host) instead of
silently succeeding or silently hanging. The control (write) paths are
exercised against a fake client injected via ``client_factory`` — never a
real network call either, matching the rest of the suite's rule of never
mocking with a working socket to a real host.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from sensibo.store import Store
from sensibo.web import WebServer

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture()
def loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block any socket connection whose target host isn't loopback."""
    orig_connect = socket.socket.connect

    def _guarded_connect(self: socket.socket, address: object, *a: object, **kw: object) -> object:
        host = address[0] if isinstance(address, tuple) else address
        if host not in _ALLOWED_HOSTS:
            raise OSError(f"network disabled for this test: attempted connect to {address!r}")
        return orig_connect(self, address, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)


class _FakeClient:
    """Mirrors tests/test_cli_set.py's _FakeClient: records calls, mutates in-memory acState."""

    def __init__(self, pods: dict[str, dict[str, Any]]) -> None:
        self._pods = {pod_id: dict(state) for pod_id, state in pods.items()}
        self.calls: list[str] = []

    def get_pod(self, pod_id: str, fields: str | None = None) -> dict[str, Any]:
        self.calls.append("get_pod")
        return {"result": {"acState": dict(self._pods[pod_id])}}

    def patch_ac_state(
        self, pod_id: str, prop: str, current_ac_state: dict[str, Any], new_value: object
    ) -> dict[str, Any]:
        self.calls.append("patch_ac_state")
        self._pods[pod_id][prop] = new_value
        return {"result": dict(self._pods[pod_id])}

    def post_ac_states(self, pod_id: str, ac_state: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("post_ac_states")
        self._pods[pod_id].update(ac_state)
        return {"result": dict(self._pods[pod_id])}


_TOKEN = "unit-test-token-value"


def _seed(db_path: Path) -> None:
    """One pod (aliased) with a 3-point temperature series, a nested Room
    Sensor, and a second, long-stale pod.

    Timestamps are anchored to the real "now" (not small epoch offsets like
    ``1_000_000.0``, ~1970) because staleness (``is_stale``) compares against
    the real current time by default — a small epoch offset would read as
    decades stale regardless of intent.
    """
    now = time.time()
    with Store(db_path=db_path) as store:
        store.upsert_location(
            "pod-1",
            kind="pod",
            product_model="elements",
            room_name="Living Room",
            seen_at=now,
        )
        store.set_alias("pod-1", "Lounge")
        store.record_reading("pod-1", "temperature", 20.0, timestamp=now - 120)
        store.record_reading("pod-1", "temperature", 21.0, timestamp=now - 60)
        store.record_reading("pod-1", "temperature", 22.0, timestamp=now)
        store.record_reading("pod-1", "humidity", 55.0, timestamp=now)
        store.upsert_location(
            "ms_abc",
            kind="room_sensor",
            parent_pod_id="pod-1",
            room_name="Living Room",
            seen_at=now,
        )
        store.record_reading("ms_abc", "temperature", 19.5, timestamp=now)
        # Seen once, decades ago -> always stale regardless of "now".
        store.upsert_location("pod-stale", kind="pod", product_model="airq", seen_at=1.0)


@pytest.fixture()
def running_server(tmp_path: Path, loopback_only: None):
    db = tmp_path / "sensibo.db"
    _seed(db)
    fake = _FakeClient({"pod-1": {"on": False, "mode": "heat", "targetTemperature": 20}})
    srv = WebServer(
        ("127.0.0.1", 0),
        db_path=str(db),
        token=_TOKEN,
        client_factory=lambda: fake,
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, fake
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


def _url(srv: WebServer, path: str) -> str:
    _host, port = srv.server_address[:2]
    return f"http://127.0.0.1:{port}{path}"


def _get(srv: WebServer, path: str) -> tuple[int, str]:
    try:
        with urlopen(_url(srv, path), timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except HTTPError as err:
        return err.code, err.read().decode("utf-8")


def _post(
    srv: WebServer, path: str, data: dict[str, str], *, headers: dict[str, str] | None = None
) -> tuple[int, str]:
    body = urlencode(data).encode("utf-8")
    req = Request(
        _url(srv, path),
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})},
    )
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except HTTPError as err:
        return err.code, err.read().decode("utf-8")


# --- pages: reads are open, and need zero cloud -----------------------------


def test_index_lists_alias_and_flags_the_stale_location(running_server) -> None:
    srv, _fake = running_server
    status, body = _get(srv, "/")
    assert status == 200
    assert "Lounge" in body  # alias wins over the Sensibo room name
    assert "STALE" in body  # pod-stale, last seen at t=1.0


def test_location_page_shows_latest_readings_and_history_svg(running_server) -> None:
    srv, _fake = running_server
    status, body = _get(srv, "/location/pod-1")
    assert status == 200
    assert "temperature" in body
    assert "humidity" in body
    assert "<svg" in body
    assert "<polyline" in body
    # Find the <h3>temperature</h3> section's own sparkline specifically —
    # humidity (only 1 seeded point) has its own chart too, and history
    # sections render in alphabetical field order (humidity before
    # temperature), so "the first points= in the page" would be humidity's.
    temperature_section = body.split("<h3>temperature</h3>")[1]
    match = re.search(r'points="([^"]+)"', temperature_section)
    assert match is not None
    # 3 seeded temperature readings for pod-1 -> 3 coordinate pairs.
    assert len(match.group(1).split()) == 3


def test_location_page_has_a_control_form_for_a_pod_but_not_a_room_sensor(running_server) -> None:
    srv, _fake = running_server
    _status, pod_body = _get(srv, "/location/pod-1")
    assert 'action="/control"' in pod_body

    _status, sensor_body = _get(srv, "/location/ms_abc")
    assert 'action="/control"' not in sensor_body


def test_unknown_location_page_is_404(running_server) -> None:
    srv, _fake = running_server
    status, _body = _get(srv, "/location/does-not-exist")
    assert status == 404


# --- JSON API: reads are open, and need zero cloud --------------------------


def test_api_locations_json(running_server) -> None:
    srv, _fake = running_server
    status, body = _get(srv, "/api/locations")
    assert status == 200
    payload = json.loads(body)
    by_id = {loc["id"]: loc for loc in payload["locations"]}
    assert {"pod-1", "ms_abc", "pod-stale"} <= set(by_id)
    assert by_id["pod-1"]["alias"] == "Lounge"
    assert by_id["pod-1"]["display_name"] == "Lounge"
    assert by_id["pod-stale"]["stale"] is True
    assert by_id["pod-1"]["stale"] is False


def test_api_latest_json(running_server) -> None:
    srv, _fake = running_server
    status, body = _get(srv, "/api/latest?location=pod-1&field=temperature")
    assert status == 200
    payload = json.loads(body)
    assert len(payload["readings"]) == 1
    assert payload["readings"][0]["value"] == 22.0


def test_api_history_json_includes_the_full_seeded_series(running_server) -> None:
    srv, _fake = running_server
    status, body = _get(srv, "/api/history?location=pod-1&field=temperature")
    assert status == 200
    payload = json.loads(body)
    values = [r["value"] for r in payload["readings"]]
    assert values == [20.0, 21.0, 22.0]


def test_api_history_requires_location_and_field(running_server) -> None:
    srv, _fake = running_server
    status, _body = _get(srv, "/api/history?location=pod-1")
    assert status == 400


# --- control: token gating ---------------------------------------------------


def test_control_without_token_is_rejected_with_zero_writes(running_server) -> None:
    srv, fake = running_server
    status, _body = _post(srv, "/control", {"pod_id": "pod-1", "mode": "cool"})
    assert status in (401, 403)
    assert fake.calls == []


def test_control_with_wrong_token_is_rejected_with_zero_writes(running_server) -> None:
    srv, fake = running_server
    status, _body = _post(
        srv, "/control", {"pod_id": "pod-1", "mode": "cool", "token": "not-the-token"}
    )
    assert status in (401, 403)
    assert fake.calls == []


def test_control_with_token_no_confirm_is_dry_run_with_zero_writes(running_server) -> None:
    srv, fake = running_server
    status, body = _post(
        srv,
        "/control",
        {"pod_id": "pod-1", "mode": "cool", "target": "22", "token": _TOKEN},
    )
    assert status == 200
    assert "cool" in body
    # Read-only: exactly one get_pod call (to compute the diff), zero writes.
    assert fake.calls == ["get_pod"]


def test_control_with_token_and_confirm_applies_exactly_one_write(running_server) -> None:
    srv, fake = running_server
    status, body = _post(
        srv,
        "/control",
        {
            "pod_id": "pod-1",
            "mode": "cool",
            "target": "22",
            "token": _TOKEN,
            "confirm": "1",
        },
    )
    assert status == 200
    assert "Applied" in body
    # Two fields changed (mode + target) -> the multi-property POST path,
    # bracketed by the diff read and the post-apply read-back.
    assert fake.calls == ["get_pod", "post_ac_states", "get_pod"]


def test_control_single_field_change_uses_the_single_property_patch(running_server) -> None:
    srv, fake = running_server
    status, _body = _post(
        srv,
        "/control",
        {"pod_id": "pod-1", "power": "on", "token": _TOKEN, "confirm": "1"},
    )
    assert status == 200
    assert fake.calls == ["get_pod", "patch_ac_state", "get_pod"]


def test_control_no_fields_to_change_is_a_user_error(running_server) -> None:
    srv, fake = running_server
    status, _body = _post(srv, "/control", {"pod_id": "pod-1", "token": _TOKEN})
    assert status == 400
    assert fake.calls == []


def test_api_set_accepts_the_token_via_header(running_server) -> None:
    srv, fake = running_server
    status, body = _post(
        srv,
        "/api/set",
        {"pod_id": "pod-1", "power": "on", "confirm": "1"},
        headers={"X-Sensibo-Token": _TOKEN},
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["applied"] is True
    assert payload["pod_id"] == "pod-1"
    assert fake.calls == ["get_pod", "patch_ac_state", "get_pod"]


def test_api_set_without_token_is_rejected(running_server) -> None:
    srv, fake = running_server
    status, body = _post(srv, "/api/set", {"pod_id": "pod-1", "power": "on"})
    assert status in (401, 403)
    payload = json.loads(body)
    assert "error" in payload
    assert fake.calls == []
