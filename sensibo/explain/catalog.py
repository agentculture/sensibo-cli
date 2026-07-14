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

_COLLECT = """\
# sensibo collect

Poll the whole fleet on a cadence and persist every reported sensor reading into
the local time-series store — the retention pillar. One cycle is a single
`GET /users/me/pods?fields=*` call (never one request per device), so it stays
within Sensibo's rate limit.

`collect` reads from the cloud and writes only to the local store; it never
drives an AC, so it has no `--apply` gate. Readings are stored under the API's
own reading times, so re-collecting an overlapping window is idempotent.

Two Sensibo traps are handled: `pm25` is stored with a unit derived from the
pod's `productModel` (AQI enum on Pure, micrograms on Elements), and each Room
Sensor is persisted under its own `ms_*` id with its parent pod recorded (a Room
Sensor is not a pod — it arrives nested in `motionSensors[]`).

## First-run backfill

On a store's first cycle, `collect` probes `historicalMeasurements` per pod
through descending windows (days=730, 365, 90, 30, 7, 1), treating an HTTP 403
as "window gated, try smaller" rather than an error. It records the series from
the largest permitted window and remembers that window in the store, so later
runs skip the probe. On a non-Plus account only days=1 is typically accessible.

## Usage

    sensibo collect                     # one cycle (the default), then exit
    sensibo collect --once --json       # one cycle, machine-readable summary
    sensibo collect --daemon            # loop, polling every 90s (Ctrl-C stops)
    sensibo collect --daemon --interval 120
    sensibo collect --once --db /path/to/sensibo.db

The poll interval has a hard floor of 60s; a lower `--interval` is rejected.
Results go to stdout; progress and the backfill window are logged to stderr.
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

_QUERY = """\
# sensibo query

Offline reads from the local store. **Never touches the network** — every
answer comes from the local sqlite file `sensibo collect` populates
(`~/.sensibo/sensibo.db` by default, override with `--db` or `SENSIBO_DB`).

An empty store or an unknown location id both fail with a remediation
pointing at `sensibo collect` — that is the verb that populates what
`query` reads.

## Verbs

- `sensibo query latest [<location-id>] [--field <name>]` — latest value(s)
  per location/field; omit either to widen the match.
- `sensibo query range <location-id> --field <name> [--since ISO8601] [--until ISO8601]`
  — time-series rows, oldest first. `--since`/`--until` are **inclusive** on
  both ends.
- `sensibo query locations` — every known sensing location (pod or Room
  Sensor) with kind, model, room name, alias (if set), and last-seen.

## Usage

    sensibo query latest --json
    sensibo query latest pod-abc123 --field temperature
    sensibo query range pod-abc123 --field temperature --since 2026-01-01 --json
    sensibo query locations --json
    sensibo query latest --db /path/to/sensibo.db
"""


_ROOM = """\
# sensibo room

The room naming registry. Every sensing location — the main pod and each Room
Sensor nested under it (`docs/sensibo-api.md`, "Trap 2: Room Sensor is not a
pod") — gets an operator-chosen name here.

Name resolution (used by `room name`, and the hook later verbs like `read`,
`query`, rules, and the web dashboard adopt) tries, in order: the location's
stable id, the operator's alias, then Sensibo's own room name. Aliases win
over Sensibo room names on collision. Ambiguous or unknown names fail with a
`hint:` line listing the candidates or known ids.

**Renames never rewrite history.** Readings key on the stable id only; an
alias is a display-time label layered on top, so renaming a location still
reaches its old readings.

## Verbs

- `sensibo room list` — every known location: stable id, kind (pod / room
  sensor), model, Sensibo room name, alias, last-seen, and a STALE flag.
- `sensibo room name <location-id-or-current-name> <new-alias>` — assign a
  persistent alias. Dry-run by default; `--apply` persists.
- `sensibo room overview` — describe this noun's surface.

## Usage

    sensibo room list
    sensibo room name pod-abc123 "Living Room" --apply
    sensibo room overview --json
"""

_ROOM_LIST = """\
# sensibo room list

Lists every known sensing location from the local store: stable id, kind
(`pod` or `room_sensor`), model (`product_model`), Sensibo's own room name,
the operator alias (if set), the last-seen timestamp, and a `stale` flag when
the location hasn't been seen in more than `--stale-after` hours (default:
24).

Fails with a remediation hint ("run `sensibo collect` first") when the store
has no known locations yet.

## Usage

    sensibo room list
    sensibo room list --stale-after 12
    sensibo room list --json
"""

_ROOM_NAME = """\
# sensibo room name

Assigns a persistent local alias to a sensing location. Accepts the
location's stable id, its current alias, or Sensibo's own room name to
identify it — an ambiguous match fails, listing every candidate; an unknown
one fails, listing known ids.

Dry-run by default: prints the rename it would make and changes nothing.
`--apply` persists it via the store's `set_alias`, which never touches
historical readings — they stay keyed on the location's stable id, so a
rename never orphans history.

## Usage

    sensibo room name pod-abc123 "Living Room"          # preview only
    sensibo room name pod-abc123 "Living Room" --apply  # persists
    sensibo room name "Living Room" "Den" --apply       # rename by current name
"""

_DEVICES = """\
# sensibo devices

Lists the fleet from exactly one API call
(`GET /users/me/pods?fields=*`, never one request per device). Per pod: its
id, `productModel`, Sensibo room name, connection status, and the sensor
field names it actually reports — derived from the keys present in that
pod's own measurements, never a hardcoded schema, so a model this tool has
never seen still lists honestly.

Room Sensors are **not pods** — they are BLE satellites nested inside their
parent pod's `motionSensors[]` with a stable `ms_*` id. They are listed as
sensing locations under their parent, with their own fields and a derived
`lastSeen` (the instant of this snapshot, when the sensor reported at least
one current reading; `null`/`unknown` otherwise — Sensibo's API carries no
per-field timestamp to read this from).

Read-only.

## Usage

    sensibo devices
    sensibo devices --json

## See also

- `sensibo explain read`
"""

_READ = """\
# sensibo read <pod-or-location-id>

One snapshot of every current reading for a location, from the same
single-call fleet poll `sensibo devices` uses. Accepts either:

- a **pod** id — prints every field in that pod's own measurements, plus each
  of its nested Room Sensors' own readings (`motionSensors`); or
- a **Room Sensor** `ms_*` id — prints just that sensor's own readings.

Unknown ids fail with a `hint:` pointing at `sensibo devices`. Read-only.

## Usage

    sensibo read <id>
    sensibo read <id> --json

## See also

- `sensibo explain devices`
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
    ("collect",): _COLLECT,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
    ("set",): _SET,
    ("query",): _QUERY,
    ("query", "latest"): _QUERY,
    ("query", "range"): _QUERY,
    ("query", "locations"): _QUERY,
    ("room",): _ROOM,
    ("room", "overview"): _ROOM,
    ("room", "list"): _ROOM_LIST,
    ("room", "name"): _ROOM_NAME,
    ("devices",): _DEVICES,
    ("read",): _READ,
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
