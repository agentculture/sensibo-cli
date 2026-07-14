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

_SMARTMODE = """\
# sensibo smartmode

Climate React — Sensibo's own **server-side** threshold automation. It runs
inside Sensibo's cloud: once enabled it keeps enforcing its thresholds even
while this machine (and any local rules engine) is asleep or offline.
Every response — text and `--json` — carries an `execution: cloud (survives
local daemon sleeping)` marker so that's never ambiguous.

## Verbs

- `sensibo smartmode show <pod>` — read-only; the current Climate React
  config (`GET /pods/{id}/smartmode`).
- `sensibo smartmode enable <pod>` / `disable <pod>` — writes. **Dry-run by
  default**: prints the current config, the requested change, and does
  nothing. `--apply` commits (`PUT /pods/{id}/smartmode`).
- `sensibo smartmode overview` — describe this noun.

## Usage

    sensibo smartmode show ac1
    sensibo smartmode enable ac1            # dry-run preview only
    sensibo smartmode enable ac1 --apply    # commits
    sensibo smartmode disable ac1 --apply --json
"""

_SCHEDULE = """\
# sensibo schedule

Recurring **server-side** automation on a pod
(`/pods/{id}/schedules/` — note the trailing slash; per-schedule ops at
`/schedules/{schedule_id}/`). Schedules fire from Sensibo's cloud even while
this machine is asleep. Every response carries an
`execution: cloud (survives local daemon sleeping)` marker.

## Verbs

- `sensibo schedule list <pod>` — read-only; the schedules on a pod.
- `sensibo schedule create <pod> --time HH:MM [--days MON,TUE|all]
  [--state on|off] [--mode ...] [--target-temperature N] [--fan-level ...]
  [--raw-body JSON]` — write. **Dry-run by default**: shows the existing
  schedules and the requested new one, and calls nothing. `--apply` commits
  (`POST /pods/{id}/schedules/`). `--raw-body` overrides the friendly flags
  with an exact JSON body — Sensibo documents the endpoint, not its request
  schema.
- `sensibo schedule delete <pod> <schedule-id>` — write. Dry-run by default;
  `--apply` commits (`DELETE /pods/{id}/schedules/{schedule-id}/`).
- `sensibo schedule overview` — describe this noun.

## Usage

    sensibo schedule list ac1
    sensibo schedule create ac1 --time 22:30 --days MON,TUE,WED   # dry-run
    sensibo schedule create ac1 --time 22:30 --apply
    sensibo schedule delete ac1 sched123 --apply --json
"""

_TIMER = """\
# sensibo timer

A one-shot **server-side** countdown on a pod (`/pods/{id}/timer/` — note the
trailing slash). It fires from Sensibo's cloud even while this machine is
asleep. Every response carries an
`execution: cloud (survives local daemon sleeping)` marker.

## Verbs

- `sensibo timer show <pod>` — read-only; the current timer state.
- `sensibo timer set <pod> --minutes N --state on|off [--mode ...]
  [--target-temperature N] [--fan-level ...] [--raw-body JSON]` — write.
  **Dry-run by default**: shows the current timer and the requested one, and
  calls nothing. `--apply` commits (`PUT /pods/{id}/timer/`).
- `sensibo timer clear <pod>` — write. Dry-run by default; `--apply` commits
  (`DELETE /pods/{id}/timer/`).
- `sensibo timer overview` — describe this noun.

## Usage

    sensibo timer show ac1
    sensibo timer set ac1 --minutes 30 --state off   # dry-run
    sensibo timer set ac1 --minutes 30 --state off --apply
    sensibo timer clear ac1 --apply --json
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
    ("smartmode",): _SMARTMODE,
    ("smartmode", "overview"): _SMARTMODE,
    ("smartmode", "show"): _SMARTMODE,
    ("smartmode", "enable"): _SMARTMODE,
    ("smartmode", "disable"): _SMARTMODE,
    ("schedule",): _SCHEDULE,
    ("schedule", "overview"): _SCHEDULE,
    ("schedule", "list"): _SCHEDULE,
    ("schedule", "create"): _SCHEDULE,
    ("schedule", "delete"): _SCHEDULE,
    ("timer",): _TIMER,
    ("timer", "overview"): _TIMER,
    ("timer", "show"): _TIMER,
    ("timer", "set"): _TIMER,
    ("timer", "clear"): _TIMER,
}
