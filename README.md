# sensibo-cli

Agent and CLI for Sensibo smart-AC control at home: discover devices, collect
every sensor reading into a local store you own, and set up automated conditions
that drive the AC.

> **Unofficial community tool.** Sensibo is a trademark of Sensibo Ltd. This
> project is not affiliated with, endorsed by, or supported by them.
>
> **Status: scaffold.** The CLI today ships only its introspection verbs
> (`whoami`, `learn`, `explain`, `overview`, `doctor`). None of the AC control,
> collection, or automation features exist yet. See the
> [roadmap](docs/roadmap.md).

## What it will do

1. **Control the AC** — power, mode, target temperature, fan speed, swing;
   per device or across the house.
2. **Collect every sensor reading locally** — poll on a cadence and persist to a
   local time-series store you own and can query offline, retained past the
   window Sensibo's own cloud keeps.
3. **Automate conditions that drive the AC** — thresholds, schedules, occupancy,
   and the cross-room logic Sensibo's own engine cannot express.

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

When the API-backed verbs land they will read the key from the environment:

```bash
export SENSIBO_API_KEY=...      # from https://home.sensibo.com/me/api
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

Every command supports `--json`. Results go to stdout, errors and diagnostics to
stderr — never mixed. Exit codes: `0` success, `1` user error, `2` environment
error, `3+` reserved.

**Every write verb will be dry-run by default, with `--apply` to commit.** This
tool turns on air conditioners in someone's home; a command that acts by accident
is a bug.

## Docs

- [`docs/sensibo-api.md`](docs/sensibo-api.md) — the Sensibo API surface, what's
  confirmed vs. unverified, rate limits, and the per-model sensor traps.
- [`docs/architecture.md`](docs/architecture.md) — how the CLI is put together
  and how to add a verb.
- [`docs/roadmap.md`](docs/roadmap.md) — build order, and which automations run
  in Sensibo's cloud vs. need a daemon alive.
- [`docs/skill-sources.md`](docs/skill-sources.md) — vendored-skill provenance.
- [`CLAUDE.md`](CLAUDE.md) — conventions for working in this repo.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
