---
name: manage-ac
type: command
description: Operate the home's Sensibo ACs from the terminal: read current state and sensor data, change power/mode/target temperature/fan/swing, and automate with local rules, cloud schedules/timers, or Climate React. Use when the user asks to turn the AC on or off, set the temperature or mode, check the room temperature/humidity, or automate the AC.
---

# Manage AC (`sensibo`)

The installed `sensibo` console command (PyPI dist `sensibo-cli`) is the sole
interface to the fleet. This skill is a quick reference; the CLI's own docs are
authoritative and always current:

- `sensibo learn` — self-teaching prompt: every noun, the safety model, the
  exit-code policy.
- `sensibo explain <noun> [verb]` — detailed markdown for any path (e.g.
  `sensibo explain set`, `sensibo explain rule arm`). When unsure of a flag,
  read `explain` instead of guessing.

## One-shot wrapper (`scripts/ac.sh`)

For explicit control actions, prefer the wrapper — one command resolves the
target pod (single-pod fleets need no id) and commits in one shot:

```bash
bash .claude/skills/manage-ac/scripts/ac.sh status              # fleet (live) + latest stored readings
bash .claude/skills/manage-ac/scripts/ac.sh on                  # power on the (single) pod
bash .claude/skills/manage-ac/scripts/ac.sh off
bash .claude/skills/manage-ac/scripts/ac.sh set 22              # target 22°
bash .claude/skills/manage-ac/scripts/ac.sh mode cool
bash .claude/skills/manage-ac/scripts/ac.sh fan auto
bash .claude/skills/manage-ac/scripts/ac.sh read                # live snapshot
bash .claude/skills/manage-ac/scripts/ac.sh on --dry-run        # preview only
bash .claude/skills/manage-ac/scripts/ac.sh status --json       # single JSON document
bash .claude/skills/manage-ac/scripts/ac.sh set 22 --json      # structured result on stdout
```

The pod argument is optional (resolved from `sensibo devices --json` when the
fleet has exactly one pod) or `all` for the whole fleet; `on <pod-id>` and
`on all` both work. `--json` is accepted by every verb: results go to stdout,
diagnostics (including the "Running:" line) to stderr, and `status --json`
emits one merged JSON document. `status` makes exactly one fleet API call —
readings come from the offline store, so an empty store prints a hint instead
of polling; `read` stays the live single-location snapshot.

## Safety contract

- **Every write verb is dry-run by default; `--apply` commits.** A `set`
  without `--apply` prints what it would do and changes nothing. Show the user
  the dry-run output and commit with `--apply` only when the user asked for the
  change (an explicit "turn the AC on" is that request).
- **The wrapper is one-shot by design.** `scripts/ac.sh on|off|set|mode|fan`
  runs `sensibo set ... --apply`, so a single call changes the AC — that is the
  point of the wrapper. Use it for explicit control requests; preview with
  `--dry-run`. Do not make it preview-by-default or add a `--no-apply` flag.
- `rule arm` requires a fresh `rule dry-run` of the rule's current definition,
  and the engine enforces a minimum off-time (10-minute floor) so a rule cannot
  short-cycle a compressor.
- Every command takes `--json`; in JSON mode errors emit
  `{"code","message","remediation"}` on stderr, and stdout and stderr never
  mix. Exit codes: `0` success, `1` user error, `2` environment error,
  `3+` reserved.

## Read (always safe)

```bash
sensibo devices                    # whole fleet: pods + nested Room Sensors, one API call
sensibo read <location-id>         # one live snapshot (pod id or ms_* Room Sensor id)
sensibo query locations            # every known sensing location in the local store
sensibo query latest               # latest reading per location (offline)
sensibo query latest <loc> --field temperature
sensibo query range <loc> --field temperature --since 2026-08-01   # offline time series
sensibo room list                  # locations, flagging stale sensors
```

`query` never touches the network (local store); `read` and `devices` poll the
Sensibo cloud.

## Write (dry-run by default)

For one-shot control use the wrapper above; the raw CLI form is:

```bash
sensibo set <pod-id> --power on                              # dry-run: prints intent
sensibo set <pod-id> --mode cool --target 24 --fan auto      # dry-run
sensibo set <pod-id> --target 22 --apply                      # commits
sensibo set --all --power off                                # fleet-wide dry-run
```

`--mode` is `cool|heat|fan|dry|auto`; `--fan` and `--swing` values are
device-specific (see `sensibo explain set`). `--all` applies the same change to
every pod instead of one.

## Naming

```bash
sensibo room name <location> <alias>            # preview only
sensibo room name <location> <alias> --apply    # persist
```

A location's stable id, current alias, or Sensibo room name all identify it;
ambiguous matches fail listing every candidate. Aliases are accepted in rule
thresholds (`--when-location`) and other location arguments that accept a name,
and a rename never orphans history (readings stay keyed on the stable id).

## Automation

- **Local rules** — this machine evaluates the local store and drives the AC:

  ```bash
  sensibo rule add --name hotday --pod <pod-id> --power on --mode cool --target 23 \
      --when-location <loc> --when-field temperature --when-op '>' --when-value 28
  sensibo rule dry-run hotday     # evaluate NOW, read-only (required before arm)
  sensibo rule arm hotday
  sensibo rule run --once         # or --daemon for continuous evaluation
  sensibo rule list               # every rule with its armed / dry-run state
  sensibo rule disarm hotday
  ```

- **Cloud** — executes in Sensibo's cloud, so it holds when this machine is
  off: `sensibo schedule create ...`, `sensibo timer set ...`,
  `sensibo smartmode enable` (Climate React). All dry-run by default;
  `--apply` commits. See `sensibo explain schedule|timer|smartmode`.

Prefer `rule` for conditions that react to sensor readings on this machine;
prefer `schedule`/`timer` for time-based behaviour that must survive this
machine being off.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/ac.sh` | One-shot control wrapper: `status`, `read`, `on`, `off`, `set <temp>`, `mode <m>`, `fan <level>` (all taking `[pod\|all]` and `--json`; control verbs also take `--dry-run`). Resolves the single-pod target automatically; control verbs commit via `sensibo set ... --apply`. `status` costs one fleet API call (readings come from the offline store). |

## Red flags

- **Never** commit a write (`--apply`) without the user asking for that change;
  when in doubt, show the dry-run and ask.
- **Never** toggle power in a loop or on a timer with the one-shot verbs —
  `ac.sh on/off` is for deliberate single actions. Automated compressor
  start/stop must go through `sensibo rule`, which enforces the minimum
  off-time (10-minute floor) that the direct `set` path intentionally does
  not, so a rule cannot short-cycle a unit.
- **Never** bypass the arm contract (fresh dry-run before `rule arm`) or lower
  the minimum off-time floor.
- **Never** poll faster than ~60 s, and never loop one call per device — one
  `devices` call covers the whole fleet (unpublished rate limit).
- Don't hard-code pod ids in scripts or docs — resolve them from
  `sensibo devices --json` at run time (ids change if a device is re-paired).
