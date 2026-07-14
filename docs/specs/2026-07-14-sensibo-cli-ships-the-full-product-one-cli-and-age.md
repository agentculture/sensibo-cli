# sensibo-cli ships the full product: one CLI and agent fully controls every Sensibo AC at home, every sensor reading lands in a local time-series store with two years of history, automated conditions drive the AC, and bigger apps connect through a Python import, an MCP server, and a website on the home network

> sensibo-cli ships the full product: one CLI and agent fully controls every Sensibo AC at home, every sensor reading lands in a local time-series store with two years of history, automated conditions drive the AC, and bigger apps connect through a Python import, an MCP server, and a website on the home network.
> instruction: The README quickstart walks all pillars in order and every verb it names exists; learn/explain/overview stay accurate as verbs land.

## Audience

- The home operator and the AI agents acting for them: Claude Code and mesh agents driving the CLI in loops, MCP clients, Python importers, and household members using the LAN website.

## Before → After

- Before: The repo is a scaffold: only introspection verbs ship; control happens only in the Sensibo app; sensor history lives in the Sensibo cloud behind an accessible window that is likely about 7 days.
- After: The operator fully controls Sensibo from the CLI, sensor data is collected into a local store reaching two years of history, automated conditions drive the AC, and bigger apps connect via Python import, MCP, and a website on the network.
  - instruction: Acceptance is the success-signal walkthrough (c20) run end-to-end on the real fleet and recorded in docs.

## Why it matters

- Data the operator owns outlives the vendor window; AC control becomes scriptable by agents; automations can express what the Sensibo engine cannot: multi-room logic, air-quality-driven cooling, occupancy plus time-of-day.

## Requirements

- Full AC control: power, mode, target temperature, fan speed, swing - per device and across the whole house.
  - instruction: Implement `sensibo set <pod> --power/--mode/--target/--fan/--swing [--apply]` plus an all-devices selector; POST acStates, PATCH acStates/{property} for single-field toggles; verify by set-then-read-back.
  - honesty: Verified against a real device: each control field round-trips (set, then read back), and a write without --apply changes nothing on the unit.
- A collector polls on a cadence and persists every reading each pod reports into a local time-series store the operator can query offline.
  - instruction: Implement sensibo collect [--daemon]: poll GET /users/me/pods?fields=* with Accept-Encoding gzip on a cadence >=60s, upsert into SQLite, expose sensibo query for offline reads.
  - honesty: No hardcoded schema: the collector stores whatever fields the pod reports, branches pm25 on productModel (AQI enum on Pure vs micrograms on Elements), and captures Room Sensor readings nested in motionSensors.
- Sensor history reaches two years back.
  - instruction: On first run probe historicalMeasurements at days=730/90/30/7, backfill everything returned, then retain locally for at least two years; write the empirically found window into docs/sensibo-api.md.
  - honesty: Two years is honest: either an empirical probe shows the cloud backfill window reaches that far, or the spec explicitly redefines it as local retention of at least two years going forward plus best-effort backfill of whatever the cloud returns.
- Automated conditions drive the AC - thresholds, schedules, occupancy, multi-room logic - both by managing the server-side Climate React and schedules, and via a local rules engine.
  - instruction: Ship rule verbs (list/add/dry-run/arm/disarm) for the local engine and smartmode/schedule verbs wrapping the cloud engine; every rule declares cloud or local execution in its definition and output.
  - honesty: Every rule states whether it runs in the Sensibo cloud (survives the laptop sleeping) or in the local daemon (does not), and no rule arms without a dry-run preview of what it would do right now.
- Bigger apps connect via Python import: the sensibo package exposes a documented public API usable without the CLI.
  - instruction: Expose a documented client class in the sensibo package (list pods, read measurements, history, set state) importable with zero CLI or argparse involvement.
  - honesty: A third-party script can import sensibo, list pods, read measurements, and set AC state without touching argparse or the console command.
- Bigger apps connect via MCP: an MCP server exposes the same read and control capabilities as tools.
  - instruction: Ship sensibo mcp serve over stdio with tools for list/read/history/set; write tools take an apply flag defaulting to false, mirroring the CLI contract.
  - honesty: An MCP client can list devices, read sensors, and set AC state, and MCP write tools honor the same dry-run-then-apply contract as the CLI.
- Bigger apps connect via a website on the home network: live readings, history, and control from a browser on the LAN.
  - instruction: Ship sensibo web [--bind ADDR:PORT] on stdlib http.server: pages read from SQLite only; control POSTs route through the same client and safety contract.
  - honesty: A browser on another machine on the LAN can view live readings and history charts served entirely from the local store, and control actions go through the same safety contract.
- Safety contract: every write verb is dry-run by default and --apply commits; rules enforce a minimum off-time and hysteresis; outbound calls are rate-limited; what a rule would do right now is inspectable before it is armed.
  - honesty: A rules-engine test proves a flapping condition cannot cycle the compressor faster than the minimum off-time.

## Honesty conditions

- Every pillar named in the announcement is demonstrable end-to-end against the real fleet - none of it mocked.
- Every named audience exercises its surface in acceptance: an agent drives the CLI with --json, an MCP client connects, a script imports the package, a LAN browser loads the dashboard.
- The before-state is as described: at frame time the repo ships only introspection verbs, with no control, collect, or rules code.
- Each after-state leg (control, collection, automation, import, MCP, web) has at least one passing end-to-end demonstration recorded in the docs.
- The differentiators are real: at least one shipped rule expresses a condition Climate React cannot (cross-room motion plus temperature), and the local store answers a query offline with the cloud unreachable.
- No doc, help text, or learn output implies a LAN-local Sensibo protocol; the cloud-only finding stays in docs/sensibo-api.md with its evidence.
- test_learn_carries_the_trademark_disclaimer stays green and the README keeps the unofficial-tool disclaimer.
- The CLI grows no verbs targeting non-Sensibo devices; rules reference Sensibo pods and their sensors only.
- The store absorbs per-model heterogeneous sensor sets without schema migrations: readings keyed on pod and timestamp with flexible field storage.
- sensibo --help works with zero installed runtime dependencies; an extra only gates its own surface, never the core CLI.
- The success walkthrough is scripted in the docs and every step passes against the real fleet, not mocks.

## Success signals

- From a fresh install with only SENSIBO_API_KEY set: sensibo devices lists the fleet with per-model sensors; collect runs on a cadence and an offline query answers from the local store; a rule dry-runs, arms, and drives the AC without short-cycling; an MCP client sets a target temperature; a browser on another LAN machine shows live readings and history.

## Scope / boundaries

- Cloud transport only: there is no LAN-local Sensibo protocol (verified). Local means where the data lives, never the wire, and no doc may imply otherwise.
- Unofficial community tool. Sensibo is a trademark of Sensibo Ltd; the disclaimer stays in the README and in learn output.
- Not a general home-automation platform: no non-Sensibo devices, no Home Assistant replacement. Rules read Sensibo sensors and drive Sensibo ACs only.

## Assumptions

- One poll cycle is one GET /users/me/pods?fields=* call with gzip - never per-device loops; 429 gets backoff; cadence is at least 60 seconds.

## Decisions

- The local store is SQLite via the stdlib sqlite3 module - keeps the zero-runtime-dependency stance while giving an indexed, offline-queryable time-series.
- The core CLI stays zero-runtime-dependency; surfaces that need real deps ship as optional extras (for example sensibo-cli[mcp]); the web dashboard uses stdlib http.server.

Resolved operator decisions (recorded via `devague question`, resolved 2026-07-14):

- **Two years of history** means local retention of at least two years going
  forward; the first run backfills whatever the cloud actually returns (probe
  `days=730/90/30/7`). Cloud-side two-year backfill is *not* a requirement.
- **Web dashboard access:** open reads on the LAN, token-gated writes.
- **MCP transport:** the official `mcp` SDK behind the optional extra
  `sensibo-cli[mcp]`; the core CLI stays zero-runtime-dependency.
- **API key location:** canonical file is `~/.sensibo/.env` (operator-maintained,
  chmod 600). Resolution order: `SENSIBO_API_KEY` environment variable first,
  then `~/.sensibo/.env`. A repo-local `.env` stays gitignored and is
  transitional only - never committed, never echoed.

## Hard questions

- risk: If the accessible cloud window is really about 7 days, any collection gap (host asleep for a week) is permanently lost data - the collector needs an always-on home.

## Open / follow-up

- Always-on host for the collector and rules daemon (which machine, systemd unit, restart policy) - the deployment story.
- Energy and runtime budget conditions in rules.
- Matter or HomeKit bridges as alternative local transports.
