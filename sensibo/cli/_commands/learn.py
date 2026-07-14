"""``sensibo learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.
"""

from __future__ import annotations

import argparse

from sensibo import __version__
from sensibo.cli._output import emit_result

_DISCLAIMER = (
    "Unofficial community tool. Sensibo is a trademark of Sensibo Ltd; this "
    "project is not affiliated with, endorsed by, or supported by them."
)

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
(Python import, MCP, LAN web dashboard).

Commands
--------
  sensibo whoami             Identity from culture.yaml.
  sensibo learn              This self-teaching prompt.
  sensibo explain <path>...  Markdown docs for any noun/verb path.
  sensibo overview           Descriptive snapshot of the agent.
  sensibo doctor             Check the agent-identity invariants.
  sensibo cli overview       Describe the CLI surface itself.
  sensibo devices            List the fleet from one API call.
  sensibo read <id>          One snapshot of every current reading.
  sensibo set <pod> ...      Control the AC (dry-run; --apply commits).
  sensibo collect            Poll on a cadence into the local store.
  sensibo query ...          Offline reads from the local store.
  sensibo room ...           Name sensing locations; flag stale sensors.
  sensibo rule ...           Local rules engine (dry-run before arm).
  sensibo smartmode ...      Climate React (runs in Sensibo's cloud).
  sensibo schedule ...       Cloud schedules.
  sensibo timer ...          Cloud timers.
  sensibo mcp serve          MCP server (needs the sensibo-cli[mcp] extra).
  sensibo web                LAN dashboard: open reads, token-gated writes.

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
            "LAN web dashboard"
        ),
        "commands": [
            {"path": ["whoami"], "summary": "Identity probe from culture.yaml."},
            {"path": ["learn"], "summary": "Self-teaching prompt."},
            {"path": ["explain"], "summary": "Markdown docs by path."},
            {"path": ["overview"], "summary": "Descriptive snapshot of the agent."},
            {"path": ["doctor"], "summary": "Check the agent-identity invariants."},
            {"path": ["cli", "overview"], "summary": "Describe the CLI surface."},
            {"path": ["devices"], "summary": "List the fleet from one API call."},
            {
                "path": ["read"],
                "summary": "One snapshot of every current reading for a location.",
            },
            {
                "path": ["set"],
                "summary": "Control the AC: dry-run by default, --apply commits.",
            },
            {
                "path": ["collect"],
                "summary": "Poll the fleet on a cadence into the local store.",
            },
            {"path": ["query"], "summary": "Offline reads from the local store."},
            {
                "path": ["room"],
                "summary": "Name sensing locations; flag stale sensors.",
            },
            {
                "path": ["rule"],
                "summary": "Local rules engine: dry-run before arm, hysteresis.",
            },
            {
                "path": ["smartmode"],
                "summary": "Climate React — runs in Sensibo's cloud.",
            },
            {"path": ["schedule"], "summary": "Cloud schedules."},
            {"path": ["timer"], "summary": "Cloud timers."},
            {
                "path": ["mcp", "serve"],
                "summary": "MCP server (needs the sensibo-cli[mcp] extra).",
            },
            {
                "path": ["web"],
                "summary": "LAN dashboard: open reads, token-gated writes.",
            },
        ],
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
