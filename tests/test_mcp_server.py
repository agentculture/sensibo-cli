"""Tests for ``sensibo.mcp_server`` — the MCP server wiring itself (task t11).

Three things under test, on top of ``tests/test_mcp_tools.py`` (which proves
each tool *function*'s behaviour in isolation):

1. **Tool registration** — :func:`sensibo.mcp_server.build_server` exposes
   exactly the five documented tools, with the expected required parameters
   in their JSON schemas.
2. **Dispatch through the real MCP machinery** — calling a tool through
   ``FastMCP.call_tool`` (not just the bare Python function) against a
   mocked client + a ``tmp_path`` store, proving ``set_ac_state``'s
   apply-defaults-to-false contract survives the trip through the SDK, not
   just in the plain function.
3. **An end-to-end stdio-shaped round trip** — a real ``ClientSession``
   talking to the server over the SDK's in-memory transport
   (``mcp.shared.memory.create_connected_server_and_client_session``): lists
   tools, then calls one. This is the practical stand-in for a literal stdio
   pipe the SDK's own test suite uses for the same purpose.

Plus the core-zero-dependency guard: ``pyproject.toml``'s ``[project]``
``dependencies`` must stay ``[]`` no matter what this task adds — only
``[project.optional-dependencies].mcp`` may pull in the real SDK.
"""

from __future__ import annotations

import asyncio
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

import sensibo.mcp_server._tools as tools
from sensibo.mcp_server import build_server, is_sdk_available
from sensibo.store import KIND_POD, Store

REPO_ROOT = Path(__file__).resolve().parents[1]

POD_ID = "pod-1"
FLEET_PAYLOAD = {
    "result": [
        {
            "id": POD_ID,
            "productModel": "airq",
            "room": {"name": "Office"},
            "connectionStatus": {"isAlive": True},
            "measurements": {"temperature": 22.0},
        }
    ]
}


class _FakeFleetClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fleet_snapshot(self, fields: str = "*") -> object:
        self.calls.append("fleet_snapshot")
        return FLEET_PAYLOAD


class _FakeAcStateClient:
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


def _run(coro):
    return asyncio.run(coro)


# --- SDK availability seam ---------------------------------------------------


def test_is_sdk_available_is_true_in_this_dev_environment() -> None:
    # `mcp` is a dev-group dependency (pyproject.toml) specifically so this
    # environment can exercise the "installed" path end-to-end.
    assert is_sdk_available() is True


def test_is_sdk_available_false_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "mcp", None)
    assert is_sdk_available() is False


# --- tool registration: names + schemas --------------------------------------


def test_build_server_registers_the_five_documented_tools() -> None:
    server = build_server()
    registered = _run(server.list_tools())
    names = {t.name for t in registered}
    assert names == {
        "list_devices",
        "read_location",
        "query_history",
        "set_ac_state",
        "room_list",
    }


def test_set_ac_state_schema_requires_only_pod_id(monkeypatch: pytest.MonkeyPatch) -> None:
    server = build_server()
    registered = {t.name: t for t in _run(server.list_tools())}

    schema = registered["set_ac_state"].inputSchema
    assert schema["required"] == ["pod_id"]
    assert set(schema["properties"]) >= {
        "pod_id",
        "power",
        "mode",
        "target",
        "fan",
        "swing",
        "apply",
    }


def test_query_history_schema_requires_only_location() -> None:
    server = build_server()
    registered = {t.name: t for t in _run(server.list_tools())}

    schema = registered["query_history"].inputSchema
    assert schema["required"] == ["location"]


def test_list_devices_schema_has_no_required_parameters() -> None:
    server = build_server()
    registered = {t.name: t for t in _run(server.list_tools())}

    schema = registered["list_devices"].inputSchema
    assert not schema.get("required")


# --- dispatch through FastMCP.call_tool: mocked client + tmp store ----------


def test_call_tool_list_devices_dispatches_to_the_tool_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeFleetClient()
    monkeypatch.setattr(tools, "SensiboClient", lambda *a, **kw: fake)
    server = build_server()

    content, structured = _run(server.call_tool("list_devices", {}))

    assert fake.calls == ["fleet_snapshot"]
    assert structured["devices"][0]["id"] == POD_ID
    assert content  # non-empty text content block too


def test_call_tool_set_ac_state_apply_false_performs_zero_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAcStateClient({"pod-1": {"on": False, "mode": "heat"}})
    monkeypatch.setattr(tools, "SensiboClient", lambda *a, **kw: fake)
    server = build_server()

    _content, structured = _run(
        server.call_tool("set_ac_state", {"pod_id": "pod-1", "mode": "cool"})
    )

    assert structured["apply"] is False
    assert fake.calls == ["get_pod"]  # one read, zero writes
    assert structured["changes"] == {"mode": {"from": "heat", "to": "cool"}}


def test_call_tool_set_ac_state_apply_true_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAcStateClient({"pod-1": {"on": False, "mode": "heat"}})
    monkeypatch.setattr(tools, "SensiboClient", lambda *a, **kw: fake)
    server = build_server()

    _content, structured = _run(
        server.call_tool("set_ac_state", {"pod_id": "pod-1", "mode": "cool", "apply": True})
    )

    assert structured["apply"] is True
    assert "patch_ac_state" in fake.calls
    assert fake._pods["pod-1"]["mode"] == "cool"


def test_call_tool_room_list_reads_the_tmp_store(tmp_path: Path) -> None:
    db_path = tmp_path / "sensibo.db"
    with Store(db_path=db_path) as store:
        store.upsert_location(POD_ID, kind=KIND_POD, product_model="airq")
        store.set_alias(POD_ID, "Office AC")

    server = build_server()
    _content, structured = _run(server.call_tool("room_list", {"db": str(db_path)}))

    (loc,) = structured["locations"]
    assert loc["alias"] == "Office AC"


# --- end-to-end: a real ClientSession over the SDK's in-memory transport ----


def test_stdio_shaped_roundtrip_list_devices_and_set_ac_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real ``ClientSession`` initializes, lists tools, and calls two of them.

    Uses ``mcp.shared.memory.create_connected_server_and_client_session`` —
    the SDK's own in-memory stand-in for a literal stdio pipe (same message
    protocol, no actual subprocess/pipe plumbing), which is the practical
    approach the task calls out explicitly.
    """
    from mcp.shared.memory import create_connected_server_and_client_session

    fake = _FakeAcStateClient({"pod-1": {"on": False, "mode": "heat"}})
    monkeypatch.setattr(tools, "SensiboClient", lambda *a, **kw: fake)
    server = build_server()

    async def scenario() -> None:
        async with create_connected_server_and_client_session(server) as session:
            listed = await session.list_tools()
            assert {t.name for t in listed.tools} == {
                "list_devices",
                "read_location",
                "query_history",
                "set_ac_state",
                "room_list",
            }

            dry_run = await session.call_tool("set_ac_state", {"pod_id": "pod-1", "mode": "cool"})
            assert dry_run.isError is False
            assert dry_run.structuredContent["apply"] is False
            assert fake.calls == ["get_pod"]  # dry run: zero writes

            applied = await session.call_tool(
                "set_ac_state", {"pod_id": "pod-1", "mode": "cool", "apply": True}
            )
            assert applied.isError is False
            assert applied.structuredContent["apply"] is True
            assert "patch_ac_state" in fake.calls

    _run(scenario())


def test_stdio_shaped_roundtrip_reports_tool_errors_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.shared.memory import create_connected_server_and_client_session

    class _BrokenClient:
        def fleet_snapshot(self, fields: str = "*") -> object:
            raise RuntimeError("network is down")

    monkeypatch.setattr(tools, "SensiboClient", lambda *a, **kw: _BrokenClient())
    server = build_server()

    async def scenario() -> None:
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool("list_devices", {})
            assert result.isError is True

    _run(scenario())


# --- core zero-dependency guard ----------------------------------------------


def test_pyproject_core_dependencies_stay_empty() -> None:
    """The MCP extra must never leak into the core CLI's dependency list."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)

    assert data["project"]["dependencies"] == []
    assert data["project"]["optional-dependencies"]["mcp"]
    assert any(dep.startswith("mcp") for dep in data["project"]["optional-dependencies"]["mcp"])
