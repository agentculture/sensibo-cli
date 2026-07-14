"""``sensibo.mcp_server`` — an MCP (Model Context Protocol) server over stdio.

Ships behind the **optional** extra ``sensibo-cli[mcp]`` (task t11). The core
CLI's zero-runtime-dependency stance (``pyproject.toml``'s ``dependencies =
[]``) is a hard constraint this package must never violate: the ``mcp`` SDK
is imported **lazily, inside functions**, never at module scope — so a bare
``import sensibo``, or even ``import sensibo.cli`` and every other CLI verb,
never touches it. Only :func:`build_server`/:func:`run_stdio` do, and only
once ``sensibo mcp serve`` (:mod:`sensibo.cli._commands.mcp`) has already
confirmed the SDK is importable.

Unofficial community tool: Sensibo is a trademark of Sensibo Ltd; this
project is not affiliated with, endorsed by, or supported by them.

Five tools are exposed, implemented in :mod:`sensibo.mcp_server._tools`
(kept import-light and MCP-independent so they're trivially unit-testable):
``list_devices``, ``read_location``, ``query_history``, ``set_ac_state``,
``room_list``. ``set_ac_state``'s ``apply`` parameter **defaults to
``False``** — a dry run returns the diff of what would change and writes
nothing, exactly mirroring the CLI's ``--apply`` safety contract
(``docs/architecture.md``, "Write verbs: dry-run by default").

See ``docs/mcp.md`` for the client-configuration walkthrough (Claude Code /
Claude Desktop stdio config) and the full tool reference.
"""

from __future__ import annotations

#: Installed exactly here so both the CLI verb and this package's own docs
#: reference one string, not two that can drift apart.
MCP_INSTALL_HINT = 'pip install "sensibo-cli[mcp]"'


def is_sdk_available() -> bool:
    """True if the optional ``mcp`` SDK is importable.

    Used by ``sensibo mcp serve`` (:mod:`sensibo.cli._commands.mcp`) to
    decide whether to raise a remediation-carrying ``CliError`` or actually
    start the server. Deliberately the *only* place in this package that
    probes for the SDK without needing it to build anything.
    """
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


def build_server():
    """Construct the MCP server with every tool registered.

    Imports the ``mcp`` SDK lazily: calling this without the ``mcp`` extra
    installed raises :class:`ModuleNotFoundError`, exactly like any other
    missing import — callers that want the friendly, remediation-carrying
    error should check :func:`is_sdk_available` first (as
    ``sensibo mcp serve`` does).
    """
    from mcp.server.fastmcp import FastMCP

    from sensibo.mcp_server import _tools

    server: FastMCP = FastMCP(
        "sensibo-cli",
        instructions=(
            "Control and read Sensibo smart-AC devices. Unofficial community "
            "tool: Sensibo is a trademark of Sensibo Ltd; this server is not "
            "affiliated with, endorsed by, or supported by them. The "
            "set_ac_state tool's apply parameter defaults to false: it "
            "returns a dry-run diff of what would change and writes "
            "nothing until called again with apply=true."
        ),
    )
    server.add_tool(
        _tools.list_devices,
        name="list_devices",
        description=(
            "List the fleet: every pod and its nested Room Sensors, from one API "
            "call. Read-only."
        ),
    )
    server.add_tool(
        _tools.read_location,
        name="read_location",
        description=(
            "Current readings for one pod or Room Sensor, by stable id, operator "
            "alias, or Sensibo room name. Read-only."
        ),
    )
    server.add_tool(
        _tools.query_history,
        name="query_history",
        description=(
            "Offline reads from the local store only (never the network): latest "
            "or range, by location and field."
        ),
    )
    server.add_tool(
        _tools.set_ac_state,
        name="set_ac_state",
        description=(
            "Control an AC's power/mode/target/fan/swing. apply defaults to "
            "false: returns the dry-run diff and writes nothing until called "
            "again with apply=true."
        ),
    )
    server.add_tool(
        _tools.room_list,
        name="room_list",
        description=(
            "Every known sensing location: id, kind, model, room name, alias, "
            "and staleness. Local store only."
        ),
    )
    return server


def run_stdio() -> None:
    """Run the MCP server over stdio. Blocks until the client disconnects."""
    build_server().run(transport="stdio")
