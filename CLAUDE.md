# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`sensibo-cli` is an **unofficial community CLI and agent for controlling Sensibo
smart-AC devices at home**. Sensibo is a trademark of Sensibo Ltd; this project
is not affiliated with or endorsed by them. Keep that disclaimer in the README
and in `learn` output.

Three pillars, in build order:

1. **Control the AC** — power, mode, target temperature, fan, swing.
2. **Collect every sensor reading into a local store** — poll on a cadence and
   persist to a time-series store the operator owns and can query offline.
3. **Automate conditions that drive the AC** — thresholds, schedules,
   occupancy, and multi-room logic.

**Current state: all three pillars are shipped**, plus the integration
surfaces (`import sensibo`, an MCP server, a LAN web dashboard) and a fourth
layer built on top of the retention pillar: **sensor health, alerting, and
offline chart reports**. `sensibo collect` evaluates every location's health
after each poll cycle, persists status/transition/notification history in the
store, and dispatches alerts through a generic webhook or an operator script
(`sensibo query health`, `sensibo notify test`, `sensibo report daily|weekly`)
— all dry-run by default, all carrying the same local-execution marker rules
use. See [`docs/health.md`](docs/health.md) for the full picture: the outage
classes, thresholds, notification config, report scheduling, the schema v2
migration, and the local-execution caveat (everything in this layer stops
when `sensibo-collect.service` stops — there is no cloud fallback).

## Commands

```bash
uv sync                                  # install (incl. dev group)
uv run pytest -n auto                    # full suite, parallel
uv run pytest tests/test_cli.py -v       # one file
uv run pytest -k whoami -v               # one test by name
uv run pytest --cov=sensibo --cov-report=term   # with coverage (fail_under=60)

uv run sensibo whoami                    # NOTE: console script is `sensibo`,
uv run sensibo learn --json              #       not `sensibo-cli` (the dist name)

uv run teken cli doctor . --strict       # the agent-first rubric gate CI enforces
uv run black sensibo tests && uv run isort sensibo tests
uv run flake8 sensibo tests
uv run bandit -c pyproject.toml -r sensibo
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken"
```

The dist is `sensibo-cli`, the import package is `sensibo`, and the console
command is **`sensibo`**. All three names differ — don't assume they match.

## Architecture

Full detail in [`docs/architecture.md`](docs/architecture.md). The essentials:

**Zero runtime dependencies, deliberately.** `pyproject.toml` has
`dependencies = []`. The CLI is stdlib-only; `teken` is dev-only. Adding a
runtime dep is a real architectural decision, not a convenience — the Sensibo
cloud API is plain REST/JSON and reachable with `urllib.request` + `json` +
`gzip`. In particular, **do not add `pysensibo`**: it hard-requires `aiohttp`,
and it does not implement `historicalMeasurements` at all — the single endpoint
this product's retention thesis is built on. Read it as a reference, not a
dependency.

**Command registration.** `sensibo/cli/__init__.py` builds the parser and calls
`register(sub)` on each module in `sensibo/cli/_commands/`. A new noun/verb is a
new module exposing `register()`, wired into `_build_parser()`, plus a matching
entry in the `sensibo/explain/catalog.py` catalog — the rubric gate fails if a
command has no `explain` entry.

**Two contracts that are load-bearing** (`teken cli doctor . --strict` enforces
them, and CI runs it):

- *Error contract* — every failure raises `CliError(code, message, remediation)`
  from `sensibo/cli/_errors.py`. `main()` catches it, routes through
  `_output.emit_error`, and exits with `code`. No Python traceback ever reaches
  stderr. Even argparse errors route through this (`_CliArgumentParser.error()`).
  Exit codes: `0` success, `1` user error, `2` environment error, `3+` reserved.
- *Stream contract* — results to **stdout**, diagnostics and errors to
  **stderr**, never mixed. Every command takes `--json`.

**Identity.** `whoami`/`doctor` parse `culture.yaml` with a hand-rolled line
scanner (`whoami.find_culture_yaml`), not a YAML library — that's what keeps the
runtime dependency-free. `doctor` enforces backend-consistency via the
`_PROMPT_FILE` map: the declared backend must have its prompt file on disk.

## Identity: this agent runs on the `colleague` backend

`culture.yaml` declares `backend: colleague` with a pinned Qwen model, so
**`AGENTS.colleague.md` is the mesh resident prompt** — that is the file the
Culture mesh daemon loads. **This `CLAUDE.md` is guidance for Claude Code
sessions only.** Both files are on disk and `doctor` is green.

If you ever change `backend:` in `culture.yaml`, you must also add the matching
prompt file, or `doctor` and CI go red.

## Conventions

**Every write verb is dry-run by default; `--apply` commits.** This is mandatory,
not a nicety. Agents call this CLI in loops, and here a write turns on an air
conditioner in someone's home. A write verb without `--apply` must print exactly
what it *would* do and change nothing.

**Safety constraints on any code that drives a compressor:** enforce a minimum
off-time / hysteresis so rules cannot short-cycle a unit, rate-limit outbound
calls, and make "what would this rule do right now?" inspectable before the rule
is armed.

**Secrets.** The API key resolves as `SENSIBO_API_KEY` in the environment
first, then `~/.sensibo/.env` — the operator-maintained canonical file (chmod
600). A repo-local `.env` is gitignored and transitional only. The key is never
committed and never echoed back in output or logs. Note the Sensibo API takes
the key as a **query parameter**, so scrub URLs before logging them.

**Every PR bumps the version** — including docs-only PRs. The `version-check` CI
job fails the run otherwise.

```bash
python3 .claude/skills/version-bump/scripts/bump.py patch|minor|major
```

**Gates that must stay green:** `pytest -n auto`, `teken cli doctor . --strict`,
markdownlint, black/isort/flake8/bandit, and the SonarCloud quality gate.

**Skills are vendored, not imported.** `.claude/skills/` is owned here,
cite-don't-import from guildmaster. Provenance lives in
[`docs/skill-sources.md`](docs/skill-sources.md). Don't reformat them —
markdownlint already ignores that tree.

**Reach for `ask-colleague` reflexively** — `review` for a second opinion on a
committed diff before opening a PR, `explore` for a fresh read of an unfamiliar
area. Both are read-only and run in a throwaway worktree, so they're always safe.
The side-effecting `write --apply` / `--pr` needs the user's go-ahead.

## The Sensibo API: what is settled

[`docs/sensibo-api.md`](docs/sensibo-api.md) is the authoritative reference and
records confidence levels per claim. The load-bearing facts:

**There is no LAN-local API. Sensibo is cloud-only.** This was verified, not
assumed (Home Assistant's integration is `iot_class: cloud_polling`; `pysensibo`
contains zero local code paths; the official OpenAPI spec declares a single
server). So **"collect locally" means the data lands and lives on the operator's
machine** — a local store they own and can query offline — *not* that the
transport is LAN-only. Do not let any doc imply a local protocol we don't have.

**Poll with one call, not one per device.** `GET /users/me/pods?fields=*`
embeds each device's measurements in a single response. Looping per-device blows
the (real, but unpublished) rate limit. Always send `Accept-Encoding: gzip` —
Sensibo documents it as a rate-limit lever. Handle `429` with backoff, and don't
poll faster than ~60s.

**Two traps that will silently corrupt collected data:**

- **`pm25` is polymorphic.** On the Pure model it is an *AQI enum* (0–3); on
  Elements it is µg/m³. Same JSON key, different units. Branch on
  `productModel` before storing, or your history is garbage.
- **Room Sensor is not a pod.** It's a BLE satellite with no pod ID, surfacing
  nested inside its parent Air/Air Pro as `motionSensors[]`.

Sensor sets differ per model — design the collector around "whatever fields this
pod reports", never a hardcoded schema.

## Naming: three names, and they don't match

| | |
|---|---|
| PyPI dist / agent nick (`culture.yaml` suffix) | `sensibo-cli` |
| import package | `sensibo` |
| **console command** | **`sensibo`** |

The rule, and it's load-bearing: **anything that names a command the user types
must say `sensibo`** — argparse `prog`, `usage:` output, `hint:` remediations,
every example in `learn` and the `explain` catalog. Naming the dist there tells
people to run `sensibo-cli --help`, which pip never installs. Tests
`test_usage_names_the_installed_command` and
`test_parse_error_hint_names_the_installed_command` guard this.

**Anything naming the agent or the project** — `whoami`'s nick, the global
`overview` subject, the `explain` root title — correctly stays `sensibo-cli`.

## `learn` must carry the trademark disclaimer

The unofficial-tool / trademark disclaimer is required in **both** the README and
`learn` output (text and `--json`), not just the README. Guarded by
`test_learn_carries_the_trademark_disclaimer`. Keep `learn`, `explain`, and
`overview` accurate as verbs land — they are the agent-facing docs, and a stale
one is a lie the next agent will act on.
