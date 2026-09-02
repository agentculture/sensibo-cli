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

import http.client
import json
import os
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

from sensibo.health.model import STATUS_DOWN, STATUS_OK, HealthConfig
from sensibo.mcp_server import _tools as mcp_tools
from sensibo.store import Store
from sensibo.store.rooms import is_stale
from sensibo.web import WebServer
from sensibo.web._wire import location_to_dict
from sensibo.web.server import (
    _DEFAULT_API_HISTORY_LIMIT,
    ENV_REPORTS_DIR,
    MAX_POST_BYTES,
)

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture
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


@pytest.fixture
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


def _raw_post(
    srv: WebServer,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> tuple[int, bytes]:
    """POST with full control over headers, including omitting
    Content-Length entirely -- `urlopen`/`Request` always compute and send a
    correct one, so the missing/invalid/oversized Content-Length paths
    (Qodo 3581287840) need this lower-level client instead.
    """
    _host, port = srv.server_address[:2]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("POST", path)
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


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


# --- bounded reads: large history stays bounded (Qodo review 3581287838) ---


def test_location_page_bounds_history_when_the_store_holds_thousands_of_readings(
    running_server,
) -> None:
    srv, _fake = running_server
    now = time.time()
    cadence = 90.0  # the real collector's ~90s poll cadence
    count = 1500  # ~37.5h of history at that cadence -- deliberately more
    # than the page's default 24h lookback window, and (before this fix)
    # enough points that a one-point-per-reading render would already be
    # unreasonably large.
    with Store(db_path=srv.db_path) as store:
        store.upsert_location("pod-big", kind="pod", product_model="elements", seen_at=now)
        for i in range(count):
            ts = now - (count - 1 - i) * cadence
            store.record_reading("pod-big", "temperature", float(i), timestamp=ts)

    status, body = _get(srv, "/location/pod-big")
    assert status == 200

    section = body.split("<h3>temperature</h3>")[1]
    match = re.search(r'points="([^"]+)"', section)
    assert match is not None
    points = match.group(1).split()
    # Downsampled to the sparkline's point cap, not one point per reading.
    assert len(points) <= 300
    # The page stays a bounded size regardless of how many readings the
    # store actually holds for this location.
    assert len(body.encode("utf-8")) < 100_000


def test_location_page_history_window_excludes_readings_older_than_the_default_lookback(
    running_server,
) -> None:
    srv, _fake = running_server
    now = time.time()
    with Store(db_path=srv.db_path) as store:
        store.upsert_location("pod-window", kind="pod", product_model="elements", seen_at=now)
        # One reading well inside the default 24h window, one well outside it.
        store.record_reading("pod-window", "temperature", 1.0, timestamp=now - 3600)
        store.record_reading("pod-window", "temperature", 999.0, timestamp=now - (72 * 3600))

    _status, body = _get(srv, "/location/pod-window")
    # The 72h-old reading must not appear anywhere in the rendered page: not
    # in "Latest readings" (it isn't the latest) and not in the sparkline
    # (outside the default lookback window).
    assert "999" not in body


def test_api_history_defaults_to_a_bounded_limit_for_a_large_series(running_server) -> None:
    srv, _fake = running_server
    now = time.time()
    count = _DEFAULT_API_HISTORY_LIMIT + 500
    with Store(db_path=srv.db_path) as store:
        store.upsert_location("pod-hist", kind="pod", product_model="elements", seen_at=now)
        for i in range(count):
            store.record_reading(
                "pod-hist", "temperature", float(i), timestamp=now - (count - 1 - i)
            )

    status, body = _get(srv, "/api/history?location=pod-hist&field=temperature")
    assert status == 200
    payload = json.loads(body)
    assert payload["limit"] == _DEFAULT_API_HISTORY_LIMIT
    assert len(payload["readings"]) == _DEFAULT_API_HISTORY_LIMIT
    # Bounded to the most *recent* readings -- the newest value survives.
    assert payload["readings"][-1]["value"] == float(count - 1)


def test_api_history_limit_query_param_is_honored(running_server) -> None:
    srv, _fake = running_server
    now = time.time()
    with Store(db_path=srv.db_path) as store:
        store.upsert_location("pod-hist2", kind="pod", product_model="elements", seen_at=now)
        for i in range(20):
            store.record_reading("pod-hist2", "temperature", float(i), timestamp=now - (20 - i))

    status, body = _get(srv, "/api/history?location=pod-hist2&field=temperature&limit=5")
    assert status == 200
    payload = json.loads(body)
    assert payload["limit"] == 5
    assert [r["value"] for r in payload["readings"]] == [15.0, 16.0, 17.0, 18.0, 19.0]


def test_api_history_limit_is_capped_regardless_of_what_the_caller_asks_for(
    running_server,
) -> None:
    srv, _fake = running_server
    status, body = _get(srv, "/api/history?location=pod-1&field=temperature&limit=999999999")
    assert status == 200
    payload = json.loads(body)
    assert payload["limit"] < 999999999


def test_api_history_rejects_a_non_positive_limit(running_server) -> None:
    srv, _fake = running_server
    status, _body = _get(srv, "/api/history?location=pod-1&field=temperature&limit=0")
    assert status == 400


def test_api_history_rejects_a_non_integer_limit(running_server) -> None:
    srv, _fake = running_server
    status, _body = _get(srv, "/api/history?location=pod-1&field=temperature&limit=abc")
    assert status == 400


def test_api_history_rejects_a_non_numeric_since(running_server) -> None:
    srv, _fake = running_server
    status, _body = _get(srv, "/api/history?location=pod-1&field=temperature&since=not-a-number")
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


# --- POST body size limits (Qodo review 3581287840) -------------------------


def test_post_missing_content_length_is_rejected_411(running_server) -> None:
    srv, fake = running_server
    status, _body = _raw_post(
        srv, "/control", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert status == 411
    assert fake.calls == []


def test_post_non_integer_content_length_is_rejected_400(running_server) -> None:
    srv, fake = running_server
    status, _body = _raw_post(
        srv,
        "/control",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": "not-a-number",
        },
    )
    assert status == 400
    assert fake.calls == []


def test_post_negative_content_length_is_rejected_400(running_server) -> None:
    srv, fake = running_server
    status, _body = _raw_post(
        srv,
        "/control",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": "-1",
        },
    )
    assert status == 400
    assert fake.calls == []


def test_post_body_over_the_max_is_rejected_413_without_reading_it(running_server) -> None:
    srv, fake = running_server
    status, _body = _raw_post(
        srv,
        "/control",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(MAX_POST_BYTES + 1),
        },
        # Deliberately zero actual body bytes: if the handler ever tried to
        # `rfile.read(length)` here, it would block waiting on bytes that
        # never arrive and this test would hang/time out instead of pass --
        # the 413 has to come back *before* any read of the body.
    )
    assert status == 413
    assert fake.calls == []


def test_post_body_at_exactly_the_max_is_accepted(running_server) -> None:
    srv, fake = running_server
    form_base = {"pod_id": "pod-1", "mode": "cool", "token": _TOKEN}
    base_body = urlencode(form_base).encode("utf-8")
    prefix = base_body + b"&pad="
    padded = prefix + b"x" * (MAX_POST_BYTES - len(prefix))
    assert len(padded) == MAX_POST_BYTES

    status, _body = _raw_post(
        srv,
        "/control",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(padded)),
        },
        body=padded,
    )
    assert status == 200
    # Read-only dry run: exactly one get_pod call, zero writes -- proves the
    # (accepted, boundary-sized) body was actually parsed correctly.
    assert fake.calls == ["get_pod"]


# --- staleness: one HealthConfig-derived source of truth (task t9) ----------


def test_room_web_and_mcp_agree_on_staleness_at_the_boundary(tmp_path: Path) -> None:
    """``room list``, the web wire format, and the MCP ``room_list`` tool must
    all read the same STALE flag for a location right at the threshold.
    """
    config = HealthConfig(down_after_seconds=1000.0)
    threshold_hours = config.down_after_seconds / 3600.0
    now = time.time()
    just_within = now - config.down_after_seconds + 5.0
    just_past = now - config.down_after_seconds - 5.0

    db = tmp_path / "boundary.db"
    with Store(db_path=db) as store:
        store.upsert_location(
            "pod-within", kind="pod", product_model="elements", seen_at=just_within
        )
        store.upsert_location("pod-past", kind="pod", product_model="elements", seen_at=just_past)
        loc_within = store.get_location("pod-within")
        loc_past = store.get_location("pod-past")

    # Surface 1: sensibo.store.rooms.is_stale -- what `sensibo room list` uses.
    assert is_stale(loc_within.last_seen, stale_after_hours=threshold_hours) is False
    assert is_stale(loc_past.last_seen, stale_after_hours=threshold_hours) is True

    # Surface 2: the web dashboard's wire format.
    within_dict = location_to_dict(loc_within, stale_after_hours=threshold_hours)
    past_dict = location_to_dict(loc_past, stale_after_hours=threshold_hours)
    assert within_dict["stale"] is False
    assert past_dict["stale"] is True

    # Surface 3: the MCP room_list tool.
    result = mcp_tools.room_list(stale_after_hours=threshold_hours, db=str(db))
    by_id = {row["id"]: row for row in result["locations"]}
    assert by_id["pod-within"]["stale"] is False
    assert by_id["pod-past"]["stale"] is True


def test_web_server_stale_after_hours_defaults_from_health_config_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENSIBO_HEALTH_DOWN_AFTER", "1800")
    srv = WebServer(
        ("127.0.0.1", 0),
        db_path=str(tmp_path / "sensibo.db"),
        token=_TOKEN,
    )
    try:
        assert srv.stale_after_hours == pytest.approx(1800.0 / 3600.0)
    finally:
        srv.server_close()


# --- health: status/since/last_ok shown, falling back to STALE (task t9) ---


def test_index_and_location_show_health_row_and_fall_back_to_stale(running_server) -> None:
    srv, _fake = running_server
    now = time.time()
    with Store(db_path=srv.db_path) as store:
        store.set_health("pod-1", status=STATUS_DOWN, since=now - 500, last_ok=now - 900)
        # pod-stale deliberately gets no health row -- it must still show the
        # derived STALE flag as a fallback.

    status, index_body = _get(srv, "/")
    assert status == 200
    assert "down" in index_body
    assert "STALE" in index_body  # pod-stale's fallback flag

    status, loc_body = _get(srv, "/location/pod-1")
    assert status == 200
    assert "down" in loc_body


def test_api_locations_carries_health_fields_when_a_row_exists(running_server) -> None:
    srv, _fake = running_server
    now = time.time()
    with Store(db_path=srv.db_path) as store:
        store.set_health("pod-1", status=STATUS_OK, since=now - 10, last_ok=now)

    status, body = _get(srv, "/api/locations")
    assert status == 200
    payload = json.loads(body)
    by_id = {loc["id"]: loc for loc in payload["locations"]}
    assert by_id["pod-1"]["health_status"] == STATUS_OK
    assert by_id["pod-1"]["health_since"] == pytest.approx(now - 10)
    assert by_id["pod-1"]["health_last_ok"] == pytest.approx(now)
    # No health row recorded for pod-stale -- falls back to None fields.
    assert by_id["pod-stale"]["health_status"] is None


def test_index_shows_the_collector_heartbeat_from_meta(running_server) -> None:
    srv, _fake = running_server
    with Store(db_path=srv.db_path) as store:
        store.set_meta("last_cycle_at", "2026-09-02T12:00:00Z")
        store.set_meta("last_cycle_outcome", "ok")

    status, body = _get(srv, "/")
    assert status == 200
    assert "2026-09-02T12:00:00Z" in body
    assert "ok" in body


def test_index_shows_never_and_unknown_heartbeat_when_no_cycle_has_run(running_server) -> None:
    srv, _fake = running_server
    status, body = _get(srv, "/")
    assert status == 200
    assert "never" in body
    assert "unknown" in body


# --- reports: serving offline SVGs from the reports directory (task t9) -----


@pytest.fixture
def reports_server(tmp_path: Path, loopback_only: None):
    db = tmp_path / "sensibo.db"
    _seed(db)
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    fake = _FakeClient({"pod-1": {"on": False, "mode": "heat", "targetTemperature": 20}})
    srv = WebServer(
        ("127.0.0.1", 0),
        db_path=str(db),
        token=_TOKEN,
        client_factory=lambda: fake,
        reports_dir=reports_dir,
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, reports_dir
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


def _write_report(reports_dir: Path, name: str, *, mtime: float | None = None) -> Path:
    path = reports_dir / name
    path.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_get_report_serves_svg_with_the_right_content_type(reports_server) -> None:
    srv, reports_dir = reports_server
    _write_report(reports_dir, "daily.svg")
    status, body = _get(srv, "/reports/daily.svg")
    assert status == 200
    assert "<svg" in body

    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    try:
        conn.request("GET", "/reports/daily.svg")
        resp = conn.getresponse()
        assert resp.getheader("Content-Type") == "image/svg+xml"
        resp.read()
    finally:
        conn.close()


def test_get_report_rejects_path_traversal_404(reports_server) -> None:
    srv, reports_dir = reports_server
    # A real secret outside the reports dir, to prove traversal never reaches it.
    secret = reports_dir.parent / "sensibo.db"
    assert secret.is_file()
    status, _body = _get(srv, "/reports/..%2Fsensibo.db")
    assert status == 404


def test_get_report_rejects_a_non_svg_name_404(reports_server) -> None:
    srv, reports_dir = reports_server
    (reports_dir / "notes.txt").write_text("not an svg", encoding="utf-8")
    status, _body = _get(srv, "/reports/notes.txt")
    assert status == 404


def test_get_report_unknown_name_404(reports_server) -> None:
    srv, _reports_dir = reports_server
    status, _body = _get(srv, "/reports/does-not-exist.svg")
    assert status == 404


def test_get_report_rejects_a_symlink_that_escapes_the_reports_dir_404(reports_server) -> None:
    """Qodo review Q8: a symlink named ``leak.svg`` inside the reports
    directory but pointing at a file outside it must 404, not follow the
    link and serve the outside file's contents."""
    srv, reports_dir = reports_server
    secret = reports_dir.parent / "sensibo.db"
    assert secret.is_file()

    link = reports_dir / "leak.svg"
    link.symlink_to(secret)

    status, body = _get(srv, "/reports/leak.svg")
    assert status == 404
    assert "sqlite" not in body.lower()


def test_index_omits_a_symlink_that_escapes_the_reports_dir(reports_server) -> None:
    """The same symlink must not appear in the index listing either."""
    srv, reports_dir = reports_server
    secret = reports_dir.parent / "sensibo.db"
    (reports_dir / "leak.svg").symlink_to(secret)
    _write_report(reports_dir, "real.svg")

    status, body = _get(srv, "/")
    assert status == 200
    assert "real.svg" in body
    assert "leak.svg" not in body


def test_index_lists_reports_newest_first(reports_server) -> None:
    srv, reports_dir = reports_server
    now = time.time()
    _write_report(reports_dir, "older.svg", mtime=now - 100)
    _write_report(reports_dir, "newer.svg", mtime=now)

    status, body = _get(srv, "/")
    assert status == 200
    assert body.index("newer.svg") < body.index("older.svg")


def test_missing_reports_dir_yields_empty_listing_not_an_error(tmp_path: Path) -> None:
    from sensibo.web.server import _list_reports

    missing = tmp_path / "does-not-exist"
    assert _list_reports(missing) == []


def test_reports_dir_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override_dir = tmp_path / "custom-reports"
    override_dir.mkdir()
    (override_dir / "a.svg").write_text("<svg></svg>", encoding="utf-8")
    monkeypatch.setenv(ENV_REPORTS_DIR, str(override_dir))

    srv = WebServer(
        ("127.0.0.1", 0),
        db_path=str(tmp_path / "sensibo.db"),
        token=_TOKEN,
    )
    try:
        assert srv.reports_dir == override_dir
    finally:
        srv.server_close()
