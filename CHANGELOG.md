# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/). This project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0] - 2026-09-02

### Added

- Sensor health layer (plan sensor-health-alerts-and-chart-reports, tasks t1-t10): store schema v2 with health, transitions, and notifications tables, a fail-closed StoreVersionError guard, and the batteryVoltage (mV) unit with a guarded backfill
- sensibo/health: pure evaluation engine — measured 900s default down threshold (91s cadence probe), collector-failure state distinct from sensor-down, parent-pod-down suppression, recovery hold, cooldown and daily cap, first-run seeding with one alert per already-down location
- sensibo/notify: generic webhook POST (redirects refused) and operator script hook (argv, no shell, timeout, API key and webhook URL stripped from the child env), webhook URL redacted everywhere, dry-run rendering
- sensibo/report: stdlib SVG daily/weekly multi-series charts, in-daemon scheduling (07:00 host-local daily, Monday weekly, SENSIBO_REPORT_* env), files under ~/.sensibo/reports served at /reports/ on the dashboard
- sensibo collect: evaluates health after every cycle, persists transitions and notification state (restart-safe exactly-once), records a heartbeat (last_cycle_at / last_cycle_outcome), survives ApiError in --daemon mode with one collector_unhealthy / collector_recovered alert pair, runs the report scheduler on both cycle paths
- New verbs: sensibo query health, sensibo notify test [--apply], sensibo report daily|weekly [--out] [--apply]; sensibo doctor gains a collector_heartbeat check and --db
- Rules: a stale leaf ({type: stale, location, after_seconds}) reading persisted health; examples/stale-room.rule.json; hysteresis, min-off-time, and rate-limit code byte-identical
- MCP: sensibo_health tool; room_list carries health fields; dashboard and MCP show status, since, last_ok, and the collector heartbeat
- docs/health.md: outage classes with reproducing SQL, states, thresholds, notification and report config, heartbeat, stale leaf, local-execution caveat, schema v2 upgrade and restart order

### Changed

- Store.record_readings commits once per poll and Store.record_series bulk-inserts one field's series in a single transaction (approved plan deviation d1; was 10.8 ms per reading)
- Staleness has one source of truth: HealthConfig.down_after_seconds drives room list, the web dashboard, and MCP; DEFAULT_STALE_AFTER_HOURS is a deprecated derived alias
- docs/sensibo-api.md: Room Sensor reports batteryVoltage (mV, quantized), not battery; records the 91s cadence probe
- CLAUDE.md and docs/architecture.md no longer describe the repo as a scaffold

### Fixed

- A recovery could be lost when the daily cap was exhausted or when the collector itself failed between a sensor's down and its recovery (colleague review of wave 1); HealthState.announced_down_since now closes every announced outage

## [0.7.4] - 2026-09-02

### Added

- docs/plans/2026-09-02-sensor-health-alerts-and-chart-reports.md: converged, exported plan (/spec-to-plan) — 11 confirmed tasks in 4 dependency waves covering all 54 spec targets, with TDD acceptance criteria, disjoint file sets per wave, and 5 plan-side risks (mixed-version upgrade window, two-writer sqlite, daemon survives ApiError, quantized batteryVoltage, manual real-fleet acceptance)

## [0.7.3] - 2026-09-02

### Added

- validate-delivery skill vendored from devague (the execution-to-evidence leg: behavioral tests agent-side, evidence and delta records via the devague CLI)
- docs/specs/2026-09-02-sensor-health-alerts-and-chart-reports.md: converged, exported, and challenged spec (/think + /challenge, rigorous pass): 40 claims, 28 honesty conditions, 25 scope entries, 6 questions resolved, 5 parks, 1 confirmed lapse. Challenge findings: collector health distinct from sensor health, parent-pod-down suppression, debounce, persisted notification state, hardened script hook, guarded schema migration, heartbeat, one staleness source of truth, a 'stale' rule leaf (reversing the notification-only decision) — 23 confirmed claims, 15 confirmed honesty conditions, 3 user decisions (SVG reports, notification-only, in-daemon scheduling)
- devague frame sensor-health-alerts-and-chart-reports: /scope survey for sensor health tracking, down detection, webhook/script notifications, and daily/weekly chart reports (17 claims, 12 scope entries with provenance, 3 open questions, 3 parked unknowns)

### Changed

- scope, challenge, summarize-delivery skills re-synced to devague 0.23.0 (subagent-aware exploration, evidence/delta/lapse-aware summary)
- docs/skill-sources.md: the devague-origin method-only skills (scope, challenge, deviate, validate-delivery, summarize-delivery) now have provenance rows; the devague-origin family is eight, not three

## [0.7.2] - 2026-08-25

### Added

- manage-ac skill: fleet quick-reference plus scripts/ac.sh one-shot wrapper (status/read/on/off/set/mode/fan, single-pod auto-resolution, --dry-run preview)
- .qwen/skills relative symlink to .claude/skills so Qwen Code discovers the same skill tree as project skills
- docs/skill-sources.md: provenance rows for the local skill and the Qwen Code bridge

## [0.7.1] - 2026-07-15

Review-triage follow-up on the always-on host (PR #5): three reliability bugs
from qodo and six SonarCloud issues, all in the new `service` code.

### Fixed

- **`collect_restart_sec` no longer truncates a fractional interval.** It used
  `int(max(...))`, so `--interval 60.5` rendered `RestartSec=60` — below the
  poll cadence, the exact hazard the floor exists to prevent. Now rounds up
  with `math.ceil` (60.5 → 61).
- **Dry-run `service install` no longer crashes on a non-systemd host.**
  `build_install_plan` probed `systemctl --version` / `loginctl show-user`
  before checking `systemd_available()`, raising `FileNotFoundError` where the
  binaries are absent. The probes are now gated behind availability; the plan
  and its "systemd is not available" warning render either way — honouring the
  dry-run-must-not-crash contract.
- **`apply_uninstall` now fails loudly instead of reporting a false success.**
  It recorded command return codes but never checked them, deleting unit files
  even when `systemctl --user disable --now` failed. It now mirrors
  `apply_install`: a non-zero result raises `ServiceError` (with a targeted
  remediation) *before* any unit file is unlinked.

### Changed

- **`_render_install_text` split into focused helpers** to bring cognitive
  complexity back under the gate; output is byte-for-byte identical.
- **`cmd_install` / `cmd_uninstall` / `cmd_status` now return `None`** rather
  than a constant `0`. The dispatcher already coerces `None` → exit 0, so
  behaviour is unchanged; this clears three Sonar "always returns the same
  value" reports. Two exception tests were tightened so only the call under
  test sits inside `pytest.raises`.

## [0.7.0] - 2026-07-14

The always-on host — closing the deployment story the spec parked as an open
follow-up ("Always-on host for the collector and rules daemon: which machine,
systemd unit, restart policy").

### Added

- **`sensibo/service/`** — systemd **user** unit lifecycle: render, install,
  status, uninstall. No root, no `/etc`, no `sudo`. Pure plan builders
  (`build_install_plan`, `build_uninstall_plan`) describe every write and
  command; `apply_install` / `apply_uninstall` are the only functions that
  mutate, so the dry-run contract is structural rather than a flag check.
- **`sensibo service`** — new noun: `install` / `status` / `uninstall` /
  `overview`. Dry-run by default (`--apply` commits), `--show-units` prints the
  full unit bodies, `--json` carries the whole inspectable plan.
  - `sensibo-collect.service` — `collect --daemon`, `Restart=always`.
    Load-bearing: `collect --daemon` exits (code 2) on any `ApiError` — a cloud
    blip, or the network not up yet at boot — and systemd is the only thing
    that brings it back. Sensibo's cloud serves only ~7 days of history, so a
    gap it fails to recover is permanently lost data.
    - **`RestartSec` is floored at the collector's own `MIN_INTERVAL` (60s)**,
      and never dips below the configured `--interval`. Because the daemon
      *exits* on an `ApiError`, a shorter restart delay would make a **failing**
      collector hit Sensibo's API **more often than a healthy one** — hammering
      an API that is by hypothesis already erroring or 429-ing us.
    - On systemd >= 254, `RestartSteps` / `RestartMaxDelaySec` back a persistent
      failure off toward 15 minutes. Omitted (not emitted-and-ignored) on older
      systemd.
    - **No start limit, deliberately.** `StartLimitBurst` would let the
      collector give up after a long outage and stay failed — which is precisely
      the data loss this unit exists to prevent. It retries forever.
  - `sensibo-web.service` — `web`, `Restart=always`.
  - `sensibo.target` — groups both, `WantedBy=default.target`.
  - `loginctl enable-linger` — starts the user manager at **boot**, not at
    login. Without it the units stop at logout and "always-on" is a lie.
    `uninstall` never disables it (the operator may rely on it elsewhere).
- **`sensibo service status`** — per-unit enabled/active, the lingering flag,
  and **how recently a reading actually landed**: `active` only proves the
  process runs; the store's freshness is the only proof collection works. The
  store section prints even when the units are absent, because "you have data
  and nothing is keeping it fresh" is exactly that operator's problem.
- **[`docs/deployment.md`](docs/deployment.md)** — the always-on host: why
  lingering is load-bearing, the API-key trap systemd's `EnvironmentFile=`
  would introduce, the `--exec-path` venv trap, and reading the journal.

### Changed

- `docs/roadmap.md` — the always-on-host open question is now answered.
- README — a "keep it running" step in the quickstart, `service` in the verb
  table, `docs/deployment.md` in the docs index.

### Security

- **No unit file ever names the API key**, and a test enforces it. Unit files
  in `~/.config/systemd/user/` are world-readable. The key resolves inside the
  client at runtime (`SENSIBO_API_KEY`, else `~/.sensibo/.env`, mode 600), so
  systemd never parses the dotenv and never logs the key — which also sidesteps
  `EnvironmentFile=` silently mangling a shell-style `.env`.
- **`rule run --daemon` is deliberately NOT installed.** It drives a compressor
  unattended; arming it stays an explicit operator decision, never a side effect
  of turning on collection. Enforced by a test that greps every written unit.

## [0.6.0] - 2026-07-14

The full product ([#1](https://github.com/agentculture/sensibo-cli/issues/1)):
all three pillars plus the integration surfaces, built from the converged
devague spec/plan (14 tasks in 4 waves, one agent per task, TDD-gated merges)
and acceptance-tested against a real fleet (`docs/walkthrough.md`).

### Added

- **`sensibo/api/`** — stdlib-only Sensibo cloud client: key resolution
  (`SENSIBO_API_KEY`, then `~/.sensibo/.env`), gzip everywhere, 429 backoff
  with jitter, client-side pacing, apiKey scrubbing on every error path, and
  thin wrappers for every documented endpoint.
- **`sensibo/store/`** — the retention thesis: SQLite time-series store,
  field-flexible per model (pm25 unit branches on `productModel`), Room
  Sensors as first-class locations, ≥2-year retention with `prune()`, offline
  queries, and operator aliases that never rewrite history.
- **Control**: `sensibo set` — dry-run by default, `--apply` commits; single
  field via PATCH, multi-field via POST, always read back.
- **Collection**: `sensibo collect` (`--once`/`--daemon`, cadence ≥60s, one
  fleet call per cycle; first run probes `historicalMeasurements` descending
  and backfills the largest permitted window — empirically `days=1` on this
  account), `sensibo query` (offline: latest/range/locations), `sensibo room`
  (list with staleness flags; `name` aliases the main unit and each Room
  Sensor by stable id).
- **Automation**: `sensibo rule` — local declarative rules engine with
  cross-room conditions resolved by room name, per-pod minimum off-time so a
  flapping condition cannot short-cycle a compressor, and an
  arm-requires-dry-run gate; `sensibo smartmode` / `schedule` / `timer` wrap
  Sensibo's cloud engine and mark every output cloud-executed.
- **Integration surfaces**: the documented `import sensibo` public API
  (`docs/api.md`, zero argparse), `sensibo mcp serve` behind the optional
  `sensibo-cli[mcp]` extra (`docs/mcp.md`), and `sensibo web` — a stdlib LAN
  dashboard with open reads and token-gated writes (`docs/web.md`).
- **`docs/walkthrough.md`** — the real-fleet acceptance record.

### Changed

- `learn`, `explain`, and the README now describe the shipped surface
  (previously: scaffold).
- One `ApiError` → `CliError` bridge (`_commands/_client.py`); HTTP 400/404
  map to user errors, everything else to environment errors.

### Fixed

- `timer/` and `schedules/` are **v1** endpoints — the OpenAPI spec's v2
  placement is wrong (v2 routes are server-level 404s; probed against the
  real fleet). `timer show` treats the application-level 404 as "no timer
  set", not an error.
- `last_seen` derives from a location's own reading time, not the poll
  instant — a Room Sensor dead since February now correctly flags STALE.
- Gzipped HTTP error bodies are decompressed before landing in error
  messages.

## [0.5.0] - 2026-07-14

Documentation release, plus the CLI self-description fixes that documenting the
real command surface exposed. No new verbs — the CLI still ships only its
introspection verbs, and no Sensibo API client exists yet.

### Added

- **`docs/sensibo-api.md`** — the authoritative API reference, and the answer to
  the build brief's load-bearing question
  ([#1](https://github.com/agentculture/sensibo-cli/issues/1)). **There is no
  LAN-local Sensibo API; the devices are cloud-only.** Verified rather than
  assumed: Home Assistant's integration is `iot_class: cloud_polling`,
  `pysensibo` contains no local code path, and the official OpenAPI spec declares
  a single server. So "collect locally" means *the data comes to rest on the
  operator's machine*, not that the transport is LAN-only — stated plainly so no
  doc implies a local protocol we don't have. Every claim is tagged
  CONFIRMED / LIKELY / UNVERIFIED, with the open questions to settle against real
  hardware listed explicitly. Records the endpoint surface, auth (the API key is
  a **query parameter** — never log a raw URL), the single-call `fields=*` poll
  pattern the unpublished-but-tight rate limit forces, and three traps: `pm25` is
  an AQI enum on Pure but µg/m³ on Elements (silent history corruption), Room
  Sensor is a BLE satellite and not a pod at all, and CO2 is derived from TVOC
  rather than measured. Also documents why `pysensibo` is a reference and not a
  dependency: it requires `aiohttp` (breaking the zero-runtime-dependency design)
  and doesn't implement `historicalMeasurements` — the one endpoint the retention
  thesis is built on.
- **`docs/architecture.md`** — the CLI skeleton, the two rubric-enforced contracts
  (the `CliError` error contract and the stdout/stderr split), how to add a verb,
  and where the `api/` / `store/` / `rules/` code will go.
- **`docs/roadmap.md`** — the build order (`devices` and `read` first), and the
  honest split between automation that runs in Sensibo's cloud (Climate React,
  schedules) and automation that needs our daemon alive. An automation that
  silently stops when a laptop sleeps is worse than no automation, so every rule
  must state where it runs.

### Changed

- **`CLAUDE.md`** — expanded from the bootstrap seed into a real runtime prompt
  (`/init`): what the agent is for, the commands, the architecture, the
  dry-run-by-default mandate for write verbs (this CLI turns on air conditioners),
  the compressor-safety constraints, and the settled Sensibo API facts.
- **`README.md`** — rewritten to describe the actual product rather than the
  template it was cloned from. Leads with the cloud-only finding and what
  "locally" therefore means.

### Fixed

- **Reconciled the `backend:` claim with what `culture.yaml` actually declares**
  (the backend-consistency invariant, per the brief). The seed `CLAUDE.md` claimed
  `backend: claude`; `culture.yaml` in fact declares **`backend: colleague`** with
  a pinned Qwen model — inherited from `culture-agent-template`, which was
  promoted to a colleague resident in its 0.3.0. The config is the truth: the mesh
  resident prompt is **`AGENTS.colleague.md`**, and `CLAUDE.md` is guidance for
  Claude Code sessions. `doctor` was already green on this (both prompt files are
  on disk); only the prose was wrong. Corrected in `CLAUDE.md` and documented in
  `docs/architecture.md`.
- **`README.md` documented the console script as `sensibo-cli`.** The actual entry
  point is **`sensibo`** (`sensibo-cli` is the PyPI dist name; `sensibo` is both
  the import package and the command). Every quickstart line was wrong.
- **The CLI told users to run a command that isn't installed.** argparse `prog`
  was `sensibo-cli`, so `sensibo --help` printed `usage: sensibo-cli …` and every
  parse-error remediation said `run 'sensibo-cli --help'` — a command pip never
  installs. `prog` is now `sensibo`, and the `explain` catalog, `learn`, `doctor`,
  and `cli overview` name the console command in every example. The agent/project
  name (`whoami`'s nick, the global `overview` subject, the `explain` root title)
  correctly remains `sensibo-cli`. Two regression tests pin the distinction.
  Surfaced by qodo on
  [#2](https://github.com/agentculture/sensibo-cli/pull/2).
- **`learn` output omitted the mandated trademark disclaimer.** The brief requires
  the unofficial-tool / Sensibo-trademark disclaimer in *both* the README and
  `learn`; it was only in the README. Now in `learn` text and in `learn --json`
  (a `disclaimer` field), with a test pinning it.
- **The CLI still described itself as "a clonable template for AgentCulture mesh
  agents"** in `learn`, `overview`, the `explain` catalog, and the parser
  description — inherited from `culture-agent-template` and inaccurate the moment
  this became an AC agent. All now describe the real tool, including the
  cloud-only/"locally" clarification and the dry-run-by-default safety note.
  `learn --json` gains `dist` and `status` fields.

## [0.4.0] - 2026-06-23

### Added

- **Vendored the `remember` + `recall` memory skills from eidetic-cli**
  (cite-don't-import) — the write/read halves of eidetic's shared
  `~/.eidetic/memory` surface, so this agent (Claude and its colleague backend)
  can persist facts across sessions and recall them later, sharing one store.
  `remember` drives `eidetic remember` (idempotent upsert of one JSON record or
  an NDJSON batch on stdin, dedup by id + content hash); `recall` drives
  `eidetic recall` with four search modes — exact / approximate / keyword /
  hybrid — each hit carrying text, full provenance metadata, a relevance score,
  and a freshness signal. The `.sh` wrappers are byte-verbatim from eidetic-cli
  (their first-party origin); each `SKILL.md` is localized only in the
  illustrative `--scope <nick>` examples (Provenance keeps "First-party to
  eidetic-cli"). Both default to this agent's PRIVATE scope, reading the suffix
  from `culture.yaml`. Runtime dep: the `eidetic` CLI on PATH (else a local
  eidetic-cli checkout with `uv`). Propagated by rollout-cli's `eidetic-memory`
  recipe.

## [0.3.4] - 2026-06-20

### Fixed

- Identity docs and self-description strings still claimed `backend: claude`
  (prompt file `CLAUDE.md`), but this template was promoted to a colleague
  resident in #14/#15: `culture.yaml` declares `backend: colleague` (Qwen) with
  `AGENTS.colleague.md` as the resident prompt. Corrected the stale claim in
  `CLAUDE.md` (Identity section), `README.md`, `docs/skill-sources.md`, and the
  two CLI description strings (`overview` artifacts and `explain doctor`). The
  `doctor` backend→prompt-file mapping and the tests were already on
  `colleague`; this aligns the prose and self-description with them.

## [0.3.3] - 2026-06-20

### Fixed

- pyproject.toml: correct the `license` field and PyPI classifier from MIT to
  Apache-2.0 to match the `LICENSE` file. The README License section was already
  corrected in 0.3.2, but the package metadata was missed; the built wheel now
  reports `License-Expression: Apache-2.0`.

## [0.3.2] - 2026-06-18

### Added

- ask-colleague skill: `monitor`/`guide`/`stop` pilot verbs plus a `--watch`
  flag to dispatch, watch the live feed of, send mid-flight guidance to, and
  cooperatively stop a running colleague flight (re-vendored from colleague).

### Changed

- README: correct the License section from MIT to Apache 2.0 to match the
  `LICENSE` file.

## [0.3.1] - 2026-06-13

### Changed

- CLAUDE.md: add a convention to reach for the `ask-colleague` skill reflexively
  for explore/review/write/grade — read-only `review`/`explore` are always safe;
  side-effecting `write` needs the user's go-ahead.

## [0.3.0] - 2026-06-13

### Added

- AGENTS.colleague.md resident prompt file (backend colleague <-> AGENTS.colleague.md)

### Changed

- Promote agent identity to a colleague resident: culture.yaml backend
  claude -> colleague with a pinned model. The `doctor` backend-consistency
  map gains `colleague` -> AGENTS.colleague.md.

## [0.2.1] - 2026-06-12

### Changed

- **Re-vendored the `ask-colleague` skill from colleague (now 1.7.0, up from the
  0.39.2 sync)** — the wrapper had drifted multiple releases behind origin. Picks
  up the `clean` verb (reap stale/corrupt `colleague/*` branches + orphaned
  `.colleague/` artifacts a crashed run left behind), the `--json` flag on every
  verb (result JSON on stdout, diagnostics/digest on stderr), the
  `_colleague_via_uv` local-dev resolution that honors `--repo`, and the
  tri-state (0/1/2) exit-code contract. `scripts/ask-colleague.sh` + `prompts/`
  are byte-identical to the origin; `SKILL.md` diverges only in the one
  consumer-identifying Provenance clause (`sensibo-cli vendors from
  guildmaster`). `docs/skill-sources.md` sync row updated to
  `2026-06-12 (colleague 1.7.0, direct)`. Refs: colleague#183, #186.

## [0.2.0] - 2026-06-06

### Added

- **`ask-colleague` skill** (`.claude/skills/ask-colleague/`) — the first-party front door to the `colleague` CLI (the renamed `convertible`). On top of `explore` / `review` / `write` it adds a `feedback` verb (grade a finished work item — the ROI loop), and `write` now **previews by default** in a throwaway worktree (no side effects) unless `--apply` / `--pr` is given. Reach for it reflexively — `review` for a diverse second opinion on a committed diff before opening a PR, `explore` for a fresh read of an unfamiliar area.

### Changed

- **Replaced the `outsource` skill with `ask-colleague`.** `outsource` was renamed to `ask-colleague` upstream ([colleague#148](https://github.com/agentculture/colleague/pull/148)). Because guildmaster has not re-broadcast the rename yet (its kit still ships the old `outsource`), `ask-colleague` is vendored **directly from the sibling `colleague` checkout** rather than from guildmaster — a tracked local divergence recorded in `docs/skill-sources.md`, parallel to the `agex` → `devex` one. Vendored verbatim except one consumer-identifying clause in the Provenance paragraph.
- **Ledger + CLAUDE.md + `.gitignore`:** point `docs/skill-sources.md` and the CLAUDE.md Skills section at `colleague` / `ask-colleague`, swap the *optional* runtime prerequisite `convertible` → `colleague` (env prefix `CONVERTIBLE_*` → `COLLEAGUE_*`, with the legacy names kept as a deprecated fallback), and gitignore the `.colleague/` run-artifact dir the skill writes (plus the stale `.agex/`).

## [0.1.4] - 2026-05-31

### Added

- **Vendor the `outsource` skill** (`.claude/skills/outsource/`) from
  guildmaster's canonical copy (origin
  [`agentculture/convertible`](https://github.com/agentculture/convertible),
  re-broadcast via guildmaster — guildmaster
  [#51](https://github.com/agentculture/guildmaster/pull/51)). Every agent
  cloned from this template now inherits the ability to hand a scoped task to a
  *different* engine/mind: `explore` (read-only investigation), `review` (a
  diverse second opinion on the committed diff), and `write` (delegate a small
  implementation). `explore`/`review` run isolated in a throwaway `git worktree`;
  `write` refuses a dirty tree. Fulfils
  [#8](https://github.com/agentculture/sensibo-cli/issues/8).
- **Ledger + CLAUDE.md:** record `outsource` in `docs/skill-sources.md`
  (origin = convertible, re-broadcast via guildmaster; vendored verbatim — it
  already carries `type: command`) and document its *optional* runtime
  dependency on the `convertible` CLI (the skill exits with an install hint if
  absent, so a clone that never uses it is unaffected).

### Changed

### Fixed

## [0.1.3] - 2026-05-31

### Changed

- Expanded the clone-and-rename instructions in `CLAUDE.md`: added `README.md` to
  the rename targets and a portable `git grep` discovery command so a cloner can
  find every occurrence of the template name (hard-coded in ~100 places across the
  package, including the CLI command files and `_ISSUES_URL` in
  `sensibo/cli/__init__.py`) rather than renaming by hand.
- Synced `README.md`'s "Make it your own" checklist with `CLAUDE.md`: it now lists
  `README.md` itself as a rename target and points to `CLAUDE.md`'s discovery
  command as the authoritative procedure, so the two onboarding checklists no
  longer drift.

## [0.1.2] - 2026-05-30

### Changed

- Renamed the PR-lifecycle CLI references `agex` / `agex-cli` to `devex` (same
  tool, new name) across `CLAUDE.md`, `docs/skill-sources.md`, `.gitignore`, and
  the vendored `cicd`, `assign-to-workforce`, and `communicate` skills — the
  `cicd` scripts now invoke `devex pr`.
- Logged the vendored-skill in-place patch as a local divergence in
  `docs/skill-sources.md`; the matching canonical rename is tracked upstream for
  guildmaster in
  [agentculture/guildmaster#48](https://github.com/agentculture/guildmaster/issues/48)
  so a future re-sync reconciles cleanly.
- Aligned the documented `devex` version floor to `>=0.21` across the vendored
  `cicd` `SKILL.md` and `workflow.sh` install hint (were `>=0.1`), matching
  `docs/skill-sources.md` and the `await`-era feature set; flagged upstream on
  guildmaster#48.

### Fixed

- SonarCloud now reports code coverage — added `relative_files = true` to
  `[tool.coverage.run]` so `coverage.xml` emits repo-relative paths that map to
  `sonar.sources=sensibo` (absolute / `.venv` paths were dropped
  as unmappable). Mirrors the sibling `convertible` setup.

## [0.1.1] - 2026-05-26

### Changed

- **CI gates on the SonarCloud quality gate**
  ([issue #3](https://github.com/agentculture/sensibo-cli/issues/3)) —
  added `sonar.qualitygate.wait=true` to `sonar-project.properties` so a failing
  gate fails the `test` job when `SONAR_TOKEN` is set. Token-less repos and fork
  PRs remain green (the scan step is guarded by `if: env.SONAR_TOKEN != ''`).

## [0.1.0] - 2026-05-26

### Added

- **Onboarded into the AgentCulture mesh** ([issue #1](https://github.com/agentculture/sensibo-cli/issues/1)).
- **Agent-first CLI** cited from teken's (`afi-cli`) `python-cli` reference
  (`teken cli cite`) — verbs `whoami`, `learn`, `explain`, `overview`, `doctor`,
  and the `cli` noun group. Runtime is self-contained (`dependencies = []`);
  `teken>=0.8` is a dev dependency only. Passes the seven-bundle agent-first
  rubric (`teken cli doctor . --strict`). `doctor` checks the agent-identity
  invariants (prompt-file-present, backend-consistency, skills-present).
- **Mesh identity**: `culture.yaml` (`suffix: sensibo-cli`,
  `backend: claude`) and the matching `CLAUDE.md` prompt file.
- **Canonical guildmaster skill kit** (11 skills) vendored under
  `.claude/skills/` (cite-don't-import): `agent-config`, `assign-to-workforce`,
  `cicd`, `communicate`, `doc-test-alignment`, `pypi-maintainer`, `run-tests`,
  `sonarclaude`, `spec-to-plan`, `think`, `version-bump`. Every `SKILL.md`
  carries `type: command` (load-bearing for the culture/claude backend);
  `cicd` / `communicate` consumer-identifying prose adapted, all script bodies
  verbatim. Provenance in `docs/skill-sources.md`. Three skills (`think`,
  `spec-to-plan`, `assign-to-workforce`) originate in `devague`, re-broadcast
  via guildmaster.
- **Build + deploy baseline**: `pyproject.toml` (hatchling), `tests/` (pytest,
  xdist, coverage), `.github/workflows/{tests,publish}.yml` (CI rubric/lint gate,
  PyPI Trusted Publishing), `.flake8`, `.markdownlint-cli2.yaml`,
  `sonar-project.properties`, and `.claude/skills.local.yaml.example`.

### Changed

### Fixed
