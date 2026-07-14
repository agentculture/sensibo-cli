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

_MCP = """\
# sensibo mcp

An MCP (Model Context Protocol) server over stdio, for bigger apps and MCP
clients (Claude Code, Claude Desktop, ...) that want the same read/control
surface as the CLI without shelling out to it. Ships behind the **optional**
extra `sensibo-cli[mcp]` — the core CLI's zero-runtime-dependency stance
(`pyproject.toml`'s `dependencies = []`) never changes; only `sensibo mcp
serve` needs the extra, and it is only imported once that verb actually runs.

Full client-configuration walkthrough and tool reference: `docs/mcp.md`.

## Verbs

- `sensibo mcp serve` — run the MCP server over stdio. Requires the `mcp`
  extra; without it, fails with a `hint:` naming
  `pip install "sensibo-cli[mcp]"` rather than a traceback.
- `sensibo mcp overview` — describe this noun.

## Tools exposed

- `list_devices` — the fleet, one API call (mirrors `sensibo devices`).
- `read_location` — current readings by stable id, alias, or room name
  (mirrors `sensibo read`, plus alias resolution via the room registry).
- `query_history` — local store only, never the network; latest/range by
  location + field (mirrors `sensibo query`).
- `set_ac_state` — power/mode/target/fan/swing. `apply` **defaults to
  `false`**: a dry run returns the diff of what would change and writes
  nothing, exactly mirroring `sensibo set` without `--apply`. `apply=true`
  commits.
- `room_list` — every known location with alias and staleness (mirrors
  `sensibo room list`).

## Usage

    pip install "sensibo-cli[mcp]"
    sensibo mcp serve
    sensibo mcp overview --json
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

_RULE = """\
# sensibo rule

The **local** rules engine: conditions over the readings already in your local
store drive an AC. This is the product's differentiator — a rule can combine
conditions **across rooms** (e.g. `motion in Hallway AND temperature in Bedroom
> 26`), addressing each location by name (stable id, operator alias, or
Sensibo's room name), which Sensibo's per-device Climate React cannot express.

**Local execution.** Every rule and every line this noun prints declares
`execution: local (stops when this daemon stops)`. That is the deliberate
contrast with the cloud verbs (`smartmode`, `schedule`, `timer`), which keep
running inside Sensibo's cloud while this machine sleeps. A local rule only acts
while `sensibo rule run` is running.

## Safety (why this noun exists)

**This drives a compressor in someone's home.** Three guards, all enforced:

- A rule is inert until **armed**, and a rule cannot be armed until a
  `rule dry-run` has evaluated its *current* definition. Editing the rule
  changes its fingerprint and invalidates the dry-run, so it must be dry-run
  again before it can re-arm.
- A per-pod **minimum off-time** (at least 10 minutes, persisted across
  restarts) refuses to flip a pod's power state again inside the window, so a
  flapping condition cannot short-cycle the compressor.
- One evaluation pass writes each pod **at most once**, through the API client's
  own rate limiting.

`rule add`/`remove`/`arm`/`disarm` edit the local rules file only and act
immediately. `rule run` is the ONLY verb that drives an AC, and only for armed
rules.

## Condition grammar

Combinators `all` / `any` / `not`; leaves:

- `{"type":"threshold","location":<name>,"field":<f>,"op":">|>=|<|<=|==|!=","value":<n>}`
- `{"type":"occupancy","location":<name>,"occupied":true,"field":<optional>}`
- `{"type":"time_window","start":"HH:MM","end":"HH:MM"}` (wraps past midnight)

## Verbs

- `sensibo rule list` — every rule with its armed / dry-run state.
- `sensibo rule add --file F` **or** `--name N --pod P --power on --mode cool
  --target 22 --when-location Bedroom --when-field temperature --when-op '>'
  --when-value 26` — define a rule (lands disarmed).
- `sensibo rule remove <name>` — delete a rule.
- `sensibo rule dry-run <name>` — evaluate NOW against the store; read-only.
- `sensibo rule arm <name>` / `disarm <name>` — activate / deactivate.
- `sensibo rule run [--once | --daemon --interval S]` — drive armed rules'
  pods. Each action is logged to stderr with the rule name.
- `sensibo rule overview` — describe this noun.

## Usage

    sensibo rule add --file examples/cross-room-motion-temp.rule.json
    sensibo rule dry-run cool-bedroom-when-hallway-busy
    sensibo rule arm cool-bedroom-when-hallway-busy
    sensibo rule run --once --json
    sensibo rule run --daemon --interval 90
"""

_WEB = """\
# sensibo web

Serves the LAN dashboard over stdlib `http.server` (`ThreadingHTTPServer`) —
zero runtime dependencies, no external assets, no JS framework. Pages and
`/api/*` JSON endpoints render **entirely from the local sqlite store**
(`sensibo/store/`): live readings per location (alias > Sensibo room name >
id), staleness flags, and inline SVG history sparklines. The dashboard works
with the Sensibo cloud unreachable; only the control form's writes need it.

**Recorded operator decision: reads are open on the LAN, writes are
token-gated.** A random token is generated on first run and persisted to
`~/.sensibo/web-token` (mode 600; override with `--token-file`) — the path is
printed to stderr, the value never is. Control POSTs (`/control`, `/api/set`)
require the token as a form field or an `X-Sensibo-Token` header, checked
with a constant-time comparison (`hmac.compare_digest`).

Submitting the control form previews the change — the same zero-write
dry-run diff as `sensibo set`, through the same `_process_pod` code path — a
second, explicit confirm submission applies it via `SensiboClient`.

## Verbs

- `sensibo web [--bind ADDR:PORT] [--db PATH] [--token-file PATH]` — serve
  the dashboard. Binds `0.0.0.0:8323` by default (LAN-reachable).

## Usage

    sensibo web                              # bind 0.0.0.0:8323
    sensibo web --bind 127.0.0.1:8323         # loopback only
    sensibo web --db /path/to/sensibo.db --token-file /path/to/token
    sensibo web --json

## Trust model

Binding `0.0.0.0` (the default) makes **reads** reachable to anyone on the
LAN — a deliberate decision recorded in the product spec, not an oversight.
Bind `127.0.0.1` instead if that is unacceptable on your network. Writes
always require the token regardless of bind address. See `docs/web.md`.
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
    ("rule",): _RULE,
    ("rule", "overview"): _RULE,
    ("rule", "list"): _RULE,
    ("rule", "add"): _RULE,
    ("rule", "remove"): _RULE,
    ("rule", "dry-run"): _RULE,
    ("rule", "arm"): _RULE,
    ("rule", "disarm"): _RULE,
    ("rule", "run"): _RULE,
    ("devices",): _DEVICES,
    ("read",): _READ,
    ("mcp",): _MCP,
    ("mcp", "overview"): _MCP,
    ("mcp", "serve"): _MCP,
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
    ("web",): _WEB,
}
