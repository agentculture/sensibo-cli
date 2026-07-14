"""``sensibo mcp serve`` — an MCP (Model Context Protocol) server over stdio.

Ships behind the **optional** extra ``sensibo-cli[mcp]`` (task t11), built on
the official ``mcp`` Python SDK. The core CLI's zero-runtime-dependency
stance is a hard constraint (``pyproject.toml``'s ``dependencies = []`` stays
empty): this module never imports ``mcp`` at module scope — only inside
:func:`cmd_mcp_serve`, and only after :func:`_sdk_available` has already
confirmed it is importable. Building the parser (:func:`sensibo.cli._build_parser`,
which runs on *every* CLI invocation, whatever verb) therefore never touches
the SDK; only actually running ``sensibo mcp serve`` does.

Missing SDK: raises :class:`~sensibo.cli._errors.CliError` with a remediation
naming the extra, rather than an ``ImportError`` traceback — the same error
contract every other verb in this CLI honours. The actual tool
implementations live in :mod:`sensibo.mcp_server`; this module is just the
CLI-verb wiring (argument parsing, the missing-SDK check, dispatch).
"""

from __future__ import annotations

import argparse

from sensibo.cli._commands.overview import emit_overview
from sensibo.cli._errors import EXIT_ENV_ERROR, CliError
from sensibo.cli._output import emit_diagnostic

_INSTALL_HINT = 'pip install "sensibo-cli[mcp]"'


def _sdk_available() -> bool:
    """True if the optional ``mcp`` SDK is importable.

    A module-level seam: tests simulate "not installed" via
    ``monkeypatch.setitem(sys.modules, "mcp", None)`` (Python's import
    machinery raises ``ImportError`` for any name mapped to ``None`` in
    ``sys.modules``) rather than actually uninstalling the dev-group SDK.
    """
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    if not _sdk_available():
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="the 'mcp' package is not installed",
            remediation=_INSTALL_HINT,
        )
    # Imported only now: sensibo.mcp_server itself also imports the SDK
    # lazily, but this second guard keeps the "never at module scope"
    # invariant obvious from this call site too.
    from sensibo.mcp_server import run_stdio

    emit_diagnostic("sensibo mcp serve: listening on stdio for an MCP client (Ctrl-C to stop)")
    run_stdio()
    return 0


def _mcp_sections() -> list[dict[str, object]]:
    return [
        {
            "title": "Verbs",
            "items": [
                "serve — run the MCP server over stdio (requires the 'mcp' extra)",
                "overview — describe this noun (this command)",
            ],
        },
        {
            "title": "Tools exposed",
            "items": [
                "list_devices — the fleet (per-model sensor fields, Room Sensor "
                "locations), one API call",
                "read_location — current readings by stable id, alias, or room name",
                "query_history — local store only; latest/range by location+field",
                "set_ac_state — apply defaults to false (dry-run diff only; apply=true commits)",
                "room_list — every known location with alias and staleness",
            ],
        },
        {
            "title": "Install",
            "items": [_INSTALL_HINT],
        },
    ]


def cmd_mcp_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "sensibo mcp",
        _mcp_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    # `sensibo mcp` with no sub-verb prints the noun's overview.
    return cmd_mcp_overview(args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "mcp",
        help="MCP (Model Context Protocol) server over stdio (see 'sensibo mcp overview').",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=_no_verb, json=False)
    # `p` is a _CliArgumentParser (propagated from the top-level subparsers'
    # parser_class), so nested subparsers built with type(p) route their own
    # parse errors through the structured error contract too.
    noun_sub = p.add_subparsers(dest="mcp_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the MCP server noun.")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_mcp_overview)

    serve = noun_sub.add_parser(
        "serve",
        help="Run the MCP server over stdio. Requires the 'mcp' extra (sensibo-cli[mcp]).",
    )
    serve.add_argument(
        "--json",
        action="store_true",
        help="Emit the missing-SDK error as structured JSON (the server itself speaks MCP, "
        "not this flag, once it starts).",
    )
    serve.set_defaults(func=cmd_mcp_serve)
