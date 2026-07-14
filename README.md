# sensibo-cli

Agent and CLI for Sensibo smart-AC control at home: discover devices, collect
every sensor reading into a local store you own, and set up automated conditions
that drive the AC.

> **Unofficial community tool.** Sensibo is a trademark of Sensibo Ltd. This
> project is not affiliated with, endorsed by, or supported by them.
>
> **Status: all three pillars shipped** — control (`set`), collection
> (`collect`/`query`/`room`), and automation (`rule` locally, plus
> `smartmode`/`schedule`/`timer` in Sensibo's cloud) — with three integration
> surfaces: `import sensibo`, MCP, and a LAN web dashboard. Acceptance-tested
> against a real fleet: [docs/walkthrough.md](docs/walkthrough.md).

## What it does

1. **Control the AC** — power, mode, target temperature, fan speed, swing;
   per device or across the house (`set`, dry-run by default).
2. **Collect every sensor reading locally** — poll on a cadence and persist to a
   local time-series store you own and can query offline, retained past the
   window Sensibo's own cloud keeps (`collect`, `query`, `room`).
3. **Automate conditions that drive the AC** — thresholds, schedules, occupancy,
   and the cross-room logic Sensibo's own engine cannot express (`rule`), plus
   scriptable front-ends for Sensibo's cloud engine (`smartmode`, `schedule`,
   `timer`).

Bigger apps connect three ways: `import sensibo` ([docs/api.md](docs/api.md)),
an MCP server ([docs/mcp.md](docs/mcp.md), optional `sensibo-cli[mcp]` extra),
and a LAN web dashboard ([docs/web.md](docs/web.md)). All three pillars were
acceptance-tested against a real fleet —
[docs/walkthrough.md](docs/walkthrough.md).

## "Locally" — read this before you assume

Sensibo devices are **cloud-only**. There is no LAN-local API: no local REST
endpoint, no MQTT, no documented on-network protocol on stock firmware. We
verified this rather than assuming it — Home Assistant's Sensibo integration is
classified `iot_class: cloud_polling`, the de-facto Python client `pysensibo`
contains no local code path, and Sensibo's official OpenAPI spec declares exactly
one server (`home.sensibo.com`).

So this tool **polls Sensibo's cloud API and persists the readings on your
machine**. "Locally" means *the data lands and lives with you* — a store you own,
queryable offline, retained on your terms — **not** that the transport avoids the
internet. We would prefer a local protocol. It does not exist.

That constraint is also the strongest argument for the local store: because there
is no device to fall back on, once a reading ages out of Sensibo's cloud history
it is gone unless you kept it.

(The one genuine exception is Apple HomeKit on Sensibo Air Pro, which really is a
LAN protocol — but it exposes only mode and target temperature, with no fan,
swing, air-quality, or history, and it pairs exclusively to one controller. It is
not a viable basis for this tool. Details in [`docs/sensibo-api.md`](docs/sensibo-api.md).)

## Scope and boundaries

Three things this project deliberately is and is not, stated plainly so no
doc, help text, or `learn` output has to be re-read to find them:

- **Cloud transport, always.** As above: Sensibo is cloud-only, there is no
  LAN-local Sensibo protocol, and "local" in this project always means *where
  the data lands* (a store you own, queryable offline) — never the wire. The
  evidence for the cloud-only finding, with sources, lives in
  [`docs/sensibo-api.md`](docs/sensibo-api.md).
- **Unofficial community tool.** See the disclaimer at the top of this file —
  Sensibo is a trademark of Sensibo Ltd, and this project is not affiliated
  with, endorsed by, or supported by them.
- **Not a general home-automation platform.** This is not a Home Assistant
  replacement and it will not grow support for non-Sensibo devices (no Tuya,
  Broadlink, Tado, Nest, Ecobee, or third-party HomeKit accessories). Every
  verb reads Sensibo sensors and drives Sensibo ACs only; a rule can combine
  readings across your Sensibo fleet, but it cannot reach outside it.

## Quickstart

```bash
uv sync
uv run pytest -n auto           # run the test suite
uv run sensibo whoami           # identity from culture.yaml
uv run sensibo learn            # self-teaching prompt (add --json)
uv run sensibo doctor           # agent-identity invariants
```

The PyPI dist is `sensibo-cli`, the import package is `sensibo`, and the console
command is **`sensibo`**.

API-backed verbs read the key from the environment (then `~/.sensibo/.env`):

```bash
export SENSIBO_API_KEY=...      # from https://home.sensibo.com/me/api
uv run sensibo devices          # list the fleet, one API call
uv run sensibo read <pod-id>    # every current reading for a location
```

The pillars, end to end:

```bash
# Control — dry-run first, --apply commits
uv run sensibo set <pod-id> --mode cool --target 24
uv run sensibo set <pod-id> --mode cool --target 24 --apply

# Collect — first run backfills what the cloud allows, then retains locally
uv run sensibo collect --once           # or --daemon for continuous collection
uv run sensibo query latest <pod-id>    # offline, from the local store
uv run sensibo room name <id> bedroom --apply   # name your sensing locations

# Automate — local rules need a dry-run before they can arm
uv run sensibo rule add --file examples/cross-room-motion-temp.rule.json
uv run sensibo rule dry-run <name> && uv run sensibo rule arm <name>
uv run sensibo rule run --daemon        # local engine; cloud verbs: smartmode,
                                        # schedule, timer

# Connect bigger apps
uv run sensibo web                      # LAN dashboard on :8323
uv run sensibo mcp serve                # needs: pip install "sensibo-cli[mcp]"
```

## CLI

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants. |
| `cli overview` | Describe the CLI surface itself. |
| `devices` | List the fleet — pods and nested Room Sensors — from one API call. |
| `read <id>` | One snapshot of every current reading for a pod or Room Sensor id. |
| `set <pod>` | Control the AC — dry-run by default, `--apply` commits. |
| `collect` | Poll the fleet on a cadence into the local store; first run backfills. |
| `query` | Offline reads from the local store: latest, range, locations. |
| `room` | Name sensing locations; flag sensors gone stale. |
| `rule` | Local rules engine: cross-room conditions, hysteresis, dry-run before arm. |
| `smartmode` | Climate React — runs in Sensibo's cloud. |
| `schedule` | Cloud schedules (survive the local daemon sleeping). |
| `timer` | Cloud one-shot timers. |
| `mcp serve` | MCP server over stdio (`sensibo-cli[mcp]` extra). |
| `web` | LAN dashboard: open reads, token-gated writes. |

Every command supports `--json`. Results go to stdout, errors and diagnostics to
stderr — never mixed. Exit codes: `0` success, `1` user error, `2` environment
error, `3+` reserved.

**Every write verb is dry-run by default, with `--apply` to commit.** This
tool turns on air conditioners in someone's home; a command that acts by accident
is a bug.

## Docs

- [`docs/sensibo-api.md`](docs/sensibo-api.md) — the Sensibo API surface, what's
  confirmed vs. unverified, rate limits, and the per-model sensor traps.
- [`docs/architecture.md`](docs/architecture.md) — how the CLI is put together
  and how to add a verb.
- [`docs/api.md`](docs/api.md) — the Python library surface: `import sensibo`
  with zero CLI/argparse involvement, for bigger apps that connect directly.
- [`docs/mcp.md`](docs/mcp.md) — the MCP server (`sensibo mcp serve`, behind
  the optional `sensibo-cli[mcp]` extra): client configuration and the tool
  reference for bigger apps that connect via MCP.
- [`docs/web.md`](docs/web.md) — the LAN dashboard: quickstart, the
  reads-open/writes-token-gated model, the token file, and the offline
  property.
- [`docs/roadmap.md`](docs/roadmap.md) — build order, and which automations run
  in Sensibo's cloud vs. need a daemon alive.
- [`docs/history.md`](docs/history.md) — the before-state this build started
  from, anchored to the frame commit.
- [`docs/skill-sources.md`](docs/skill-sources.md) — vendored-skill provenance.
- [`CLAUDE.md`](CLAUDE.md) — conventions for working in this repo.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
