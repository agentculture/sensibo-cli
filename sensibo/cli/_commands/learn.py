"""``sensibo learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.
"""

from __future__ import annotations

import argparse

from sensibo import __version__
from sensibo.cli._output import emit_result
from sensibo.explain.catalog import COMMAND_ORDER, SUMMARIES

_DISCLAIMER = (
    "Unofficial community tool. Sensibo is a trademark of Sensibo Ltd; this "
    "project is not affiliated with, endorsed by, or supported by them."
)


def _command_map_lines() -> list[str]:
    """Render the "Commands" block from the catalog's single source of truth.

    Walks :data:`sensibo.explain.catalog.COMMAND_ORDER` instead of a second,
    hand-maintained list — a manually maintained command list here is exactly
    what drifted from the real verb surface as verbs landed (Qodo review
    3581287831). A new verb adds one entry to the catalog and both this text
    block and :func:`_as_json_payload`'s ``commands`` list pick it up.
    """
    rows = [(f"sensibo {' '.join(path)}", SUMMARIES[path]) for path in COMMAND_ORDER]
    width = max(len(invocation) for invocation, _summary in rows) + 2
    return [f"  {invocation.ljust(width)}{summary}" for invocation, summary in rows]


_TEXT = f"""\
sensibo — control Sensibo smart-AC devices from the command line.

{_DISCLAIMER}

Purpose
-------
Three pillars: control the AC (power, mode, target, fan, swing); collect every
sensor reading into a local store you own and can query offline; and automate
conditions that drive the AC (thresholds, schedules, occupancy, cross-room).

Sensibo devices are cloud-only — there is no LAN-local API — so readings are
polled from Sensibo's cloud and persisted locally. "Locally" means the data
comes to rest on your machine, not that the transport avoids the internet.

STATUS: all three pillars are shipped, plus the integration surfaces
(Python import, MCP, LAN web dashboard), plus sensor health tracking
(`query health`), test notifications (`notify test`), and offline SVG reports
(`report daily`/`report weekly`, dry-run by default, `--apply` writes and
delivers) — all of which carry the local-execution marker
`execution: local (stops when this daemon stops)`: health tracking, alerting,
and the in-daemon report scheduler only run while `sensibo collect` is
running, unlike Sensibo's own cloud automation.

Commands
--------
{chr(10).join(_command_map_lines())}

Note: the console command is `sensibo`. `sensibo-cli` is the PyPI dist name.

Safety
------
Every write verb is dry-run by default; --apply commits. This tool drives
air conditioners in a home, so a command that acts by accident is a bug.
Local rules enforce a minimum off-time so they cannot short-cycle a
compressor, and a rule cannot arm without a dry-run of its current
definition.

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{{"code", "message", "remediation"}} to stderr. Stdout and stderr never mix.

Exit-code policy
----------------
  0 success
  1 user-input error (bad flag, bad path, missing arg)
  2 environment / setup error
  3+ reserved

More detail
-----------
  sensibo explain sensibo-cli
"""


def _as_json_payload() -> dict[str, object]:
    return {
        # `tool` is the command an agent invokes; `dist` is the PyPI name.
        "tool": "sensibo",
        "dist": "sensibo-cli",
        "version": __version__,
        "purpose": (
            "Control Sensibo smart-AC devices, collect their sensor readings into a "
            "local store, and automate conditions that drive the AC."
        ),
        "disclaimer": _DISCLAIMER,
        "status": (
            "all three pillars are shipped (control, collection, automation), plus the "
            "integration surfaces: Python import, MCP (sensibo-cli[mcp] extra), and the "
            "LAN web dashboard; plus sensor health tracking ('query health') and test "
            "notifications ('notify test') — both carry execution: local (stops when this "
            "daemon stops)"
        ),
        "commands": [{"path": list(path), "summary": SUMMARIES[path]} for path in COMMAND_ORDER],
        "exit_codes": {
            "0": "success",
            "1": "user-input error",
            "2": "environment/setup error",
        },
        "json_support": True,
        "explain_pointer": "sensibo explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)
