# Build Plan — sensibo-cli ships the full product: one CLI and agent fully controls every Sensibo AC at home, every sensor reading lands in a local time-series store with two years of history, automated conditions drive the AC, and bigger apps connect through a Python import, an MCP server, and a website on the home network

slug: `sensibo-cli-ships-the-full-product-one-cli-and-age` · status: `exported` · from frame: `sensibo-cli-ships-the-full-product-one-cli-and-age`

> sensibo-cli ships the full product: one CLI and agent fully controls every Sensibo AC at home, every sensor reading lands in a local time-series store with two years of history, automated conditions drive the AC, and bigger apps connect through a Python import, an MCP server, and a website on the home network.

## Tasks

### t1 — API client core: stdlib urllib client with key resolution, gzip, backoff, scrubbing

- covers: c16
- acceptance:
  - Key resolves SENSIBO_API_KEY env var first, then ~/.sensibo/.env; a test proves the order and that a missing key raises CliError with remediation
  - Every request sends Accept-Encoding: gzip; 429 triggers exponential backoff with jitter; outbound calls are rate-limited client-side
  - No apiKey value ever appears in exceptions, logs, or --json output: a test asserts URL scrubbing
  - Fleet poll is a single GET /users/me/pods?fields=* call - a test fails if any code path loops per device for current readings

### t2 — SQLite store: flexible per-model schema, 2-year retention, offline query API

- covers: c11
- acceptance:
  - Readings are keyed on pod and timestamp with flexible field storage; a pod reporting a never-seen field stores it without schema migration
  - pm25 is stored with a unit tag derived from productModel (AQI enum on Pure, micrograms on Elements); a test covers both branches
  - Retention defaults to at least two years and is configurable; pruning never touches data newer than the window
  - A store query answers with the network unreachable (test monkeypatches socket to fail)

### t3 — Boundary and positioning docs: trademark, cloud-only, scope, before-state evidence

- covers: c3, h14, c6, h16, c7, h17, c8, h18
- acceptance:
  - test_learn_carries_the_trademark_disclaimer stays green; README keeps the unofficial-tool disclaimer
  - No doc, help text, or learn output implies a LAN-local Sensibo protocol; docs/sensibo-api.md keeps the cloud-only finding with evidence
  - The before-state (scaffold with only introspection verbs at frame time) is recorded in the docs with the frame commit hash
  - A grep-based test asserts no CLI verb or rule references non-Sensibo devices

### t4 — devices and read verbs: list the fleet, snapshot every current reading

- depends on: t1
- covers: c2
- acceptance:
  - sensibo devices lists every pod with model and the sensor fields it actually reports, from one API call; --json emits machine-readable output
  - `sensibo read <pod>` prints one snapshot of every current reading including nested motionSensors from Room Sensors
  - Both verbs have explain catalog entries and teken cli doctor . --strict stays green

### t5 — set verb: full AC control, dry-run by default

- depends on: t1
- covers: c9, h1, c16
- acceptance:
  - sensibo set supports --power --mode --target --fan --swing per device plus an all-devices selector
  - Without --apply the verb prints exactly what it would change and a test proves zero HTTP writes happen; with --apply it POSTs acStates or PATCHes a single property
  - Set-then-read-back verification against a real device is scripted and passes
  - Explain catalog entry present; doctor strict green

### t6 — collect verb: cadence polling, historical backfill probe, upsert to store

- depends on: t1, t2
- covers: c10, h2, c11, h3
- acceptance:
  - sensibo collect polls GET /users/me/pods?fields=* on a cadence >=60s and upserts every reported field into the store
  - First run probes historicalMeasurements at days=730/90/30/7, backfills everything returned, and records the empirically found window in docs/sensibo-api.md
  - pm25 branches on productModel before storage; a test covers Pure vs Elements
  - A collect cycle against a mocked 429 backs off and completes; explain entry present, doctor green
  - Backfill probes descending windows and treats 403 as a gated window, not an error: it uses the largest permitted days value (empirically 1 on this account) and records the finding

### t7 — query verb: offline reads from the local store

- depends on: t2
- covers: c10, h15
- acceptance:
  - sensibo query answers time-range and latest-value questions from SQLite with the network disabled (test enforces no socket use)
  - Output is stdout-only results with --json; explain entry present, doctor green

### t8 — cloud automation verbs: smartmode, schedule, timer wrappers

- depends on: t1
- covers: c12, h4
- acceptance:
  - sensibo smartmode / schedule / timer read and write Sensibo server-side automation; writes are dry-run by default with --apply
  - Output marks these as cloud-executed (survive the local daemon sleeping)
  - Explain entries present; doctor strict green

### t9 — local rules engine: conditions over collected sensors drive the AC safely

- depends on: t2, t5
- covers: c12, h4, c16, h8, c5, h15
- acceptance:
  - Rules express thresholds, schedules, occupancy, and cross-room conditions; one shipped example combines motion in one room with temperature in another - something Climate React cannot express
  - A flapping-condition test proves the engine cannot cycle a compressor faster than the configured minimum off-time (hysteresis)
  - sensibo rule dry-run shows exactly what an unarmed rule would do right now; a rule cannot arm without it
  - Every rule declares local execution in its definition and output (contrast with cloud verbs); rules reference Sensibo pods only

### t10 — public import surface: documented client API usable without the CLI

- depends on: t1
- covers: c13, h5
- acceptance:
  - A third-party script can import sensibo, list pods, read measurements and history, and set AC state with zero argparse or CLI imports (test asserts no cli module import)
  - The public API is documented in docs with runnable examples

### t11 — MCP server behind the optional extra sensibo-cli[mcp]

- depends on: t10
- covers: c14, h6
- acceptance:
  - pip install sensibo-cli leaves the core zero-dep and sensibo --help works; the mcp extra pulls the official SDK and only gates its own surface
  - sensibo mcp serve exposes stdio tools to list devices, read sensors, read history, and set AC state; write tools take apply defaulting to false
  - An MCP client end-to-end test lists devices and sets a target temperature through the tool

### t12 — web dashboard on stdlib http.server: LAN reads open, writes token-gated

- depends on: t2, t5
- covers: c15, h7
- acceptance:
  - sensibo web --bind serves live readings and history charts rendered entirely from the local store (test: pages load with cloud unreachable)
  - Control POSTs require a token and route through the same client and dry-run safety contract; reads need no auth
  - Zero runtime deps: stdlib http.server only; a browser on another LAN machine can load the dashboard

### t13 — end-to-end acceptance: scripted walkthrough against the real fleet

- depends on: t3, t4, t6, t7, t8, t9, t10, t11, t12
- covers: c1, h11, c4, h12, c20, h19, c2, h13
- acceptance:
  - A scripted walkthrough runs devices, read, collect, query, a rule dry-run and arm, an MCP tool call, and a LAN dashboard load against the real fleet, and every step passes - none of it mocked
  - README quickstart walks all pillars in order and every verb it names exists; learn/explain/overview are accurate for every shipped verb
  - Each named audience surface is exercised: agent CLI with --json, MCP client, Python import, LAN browser

### t14 — room naming registry: operator-chosen names for the main unit and each Room Sensor

- depends on: t2
- covers: c21, h20
- acceptance:
  - sensibo room list shows every sensing location (main unit and each Room Sensor with stable id, model, last-seen); sensibo room name assigns a persistent local alias, dry-run by default
  - read, query, rules, and the web dashboard accept and display room names; a test renames a location and queries its old data by the new name without loss
  - Stale sensors are visible: room list flags a location whose last reading is older than a threshold
  - Explain catalog entries present; teken cli doctor . --strict stays green

## Risks

- [unknown_nonblocking] historicalMeasurements empirical window unknown until probed with the real key - probe read-only before t6 lands (task t6)
- [unknown_nonblocking] Real rate-limit ceiling unpublished - find empirically and gently in t1 (task t1)
- [unknown_nonblocking] catalog.py and cli/__init__.py are shared append points for every verb task - same-wave textual conflicts are mechanical (append-only) but must be merged by the operator
- [unknown_nonblocking] timer/ and schedules/ may live under v1 not v2 - verify during t8 (task t8)
- [unknown_nonblocking] Real-fleet acceptance (t5, t13) needs the operator key present at ~/.sensibo/.env - operator has staged it (task t13)
