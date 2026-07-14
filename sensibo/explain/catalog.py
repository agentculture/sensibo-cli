"""Markdown catalog for ``sensibo explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple
and ``("sensibo-cli",)`` both resolve to the root entry.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# sensibo-cli

Agent and CLI for Sensibo smart-AC control at home. Three pillars: control the
AC (power, mode, target temperature, fan, swing); collect every sensor reading
into a local store you own and can query offline; and automate conditions that
drive the AC (thresholds, schedules, occupancy, cross-room logic).

**Unofficial community tool.** Sensibo is a trademark of Sensibo Ltd; this
project is not affiliated with, endorsed by, or supported by them.

The console command is `sensibo`. `sensibo-cli` is the PyPI dist name.

## Status: scaffold

Only the introspection verbs below are implemented. The AC control, collection,
and automation verbs do not exist yet.

## "Locally"

Sensibo devices are cloud-only — there is no LAN-local API, no local REST
endpoint, and no MQTT on stock firmware. Readings are polled from Sensibo's
cloud and persisted on your machine. "Locally" describes where the data comes to
rest, not a transport that avoids the internet.

## Safety

Every write verb will be dry-run by default; `--apply` commits. This tool drives
air conditioners in a home, so a command that acts by accident is a bug.

## Verbs

- `sensibo whoami` — identity probe from `culture.yaml`.
- `sensibo learn` — structured self-teaching prompt.
- `sensibo explain <path>` — markdown docs for any noun/verb.
- `sensibo overview` — descriptive snapshot of the agent.
- `sensibo doctor` — check the agent-identity invariants.
- `sensibo cli overview` — describe the CLI surface.

## Exit-code policy

- `0` success
- `1` user-input error
- `2` environment / setup error
- `3+` reserved

## See also

- `sensibo explain whoami`
- `sensibo explain doctor`
"""

_WHOAMI = """\
# sensibo whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

## Usage

    sensibo whoami
    sensibo whoami --json
"""

_LEARN = """\
# sensibo learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    sensibo learn
    sensibo learn --json
"""

_EXPLAIN = """\
# sensibo explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    sensibo explain sensibo-cli
    sensibo explain whoami
    sensibo explain --json <path>
"""

_OVERVIEW = """\
# sensibo overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the sibling-pattern artifacts this repo carries. Accepts an
ignored `target` so a stray path never hard-fails.

## Usage

    sensibo overview
    sensibo overview --json
"""

_DOCTOR = """\
# sensibo doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`colleague` → `AGENTS.colleague.md`), plus a
skills-present check. Exits 1 when unhealthy.

## Usage

    sensibo doctor
    sensibo doctor --json
"""

_CLI = """\
# sensibo cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    sensibo cli overview
    sensibo cli overview --json
"""

_SET = """\
# sensibo set

**Drives an air conditioner in someone's home.** Dry-run by default: without
`--apply`, reads the pod's current `acState` and prints exactly what *would*
change — zero write requests. With `--apply`, a single changed field goes
through the safe single-property `PATCH`; two or more changed fields go
through the full-state `POST`. Either way the resulting state is read back and
reported, never assumed.

`--all` applies the same requested change to every pod in the fleet via one
fleet listing call; per-pod writes still only happen with `--apply`.

## Usage

    sensibo set <pod-id> --mode cool --target 22
    sensibo set <pod-id> --power on --apply
    sensibo set --all --power off --apply
    sensibo set <pod-id> --mode cool --json

## Flags

- `--power on|off`
- `--mode cool|heat|fan|dry|auto`
- `--target <temp>`
- `--fan <level>` (device-specific)
- `--swing <mode>` (device-specific)
- `--all` — target every pod in the fleet
- `--apply` — commit the change (default is dry-run)
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("sensibo-cli",): _ROOT,
    ("sensibo",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
    ("set",): _SET,
}
