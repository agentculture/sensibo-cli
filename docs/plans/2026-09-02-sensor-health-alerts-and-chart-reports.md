# Build Plan — sensor health, alerts, and chart reports

slug: `sensor-health-alerts-and-chart-reports` · status: `exported` · from frame: `sensor-health-alerts-and-chart-reports`

> sensibo-cli now watches every sensor in the home: each reading every pod and Room Sensor reports lands in the local sqlite store, a sensor that goes quiet or runs its battery down is flagged and announced on Discord, and a daily and a weekly chart of the measurements is delivered as an image report

## Tasks

### t1 — Store schema v2: health, transitions, notifications tables; fail-closed version check; batteryVoltage unit + guarded backfill; prune scope

- instruction: Files: sensibo/store/`_schema.py`, sensibo/store/`_units.py`, sensibo/store/store.py, sensibo/store/`__init__.py`, tests/`test_store.py`. Do not touch collector, CLI, web, or rules. Keep the layering: no imports from sensibo.cli or sensibo.api.
- covers: c30, h17, c11, h7, c33, h28, c2, h2
- acceptance:
  - `SCHEMA_VERSION` is 2; `init_schema` creates health(`location_id` PK, status, since, `last_ok`, `parent_pod_id`), transitions(id, `location_id`, `from_status`, `to_status`, at, `notified_at`), notifications(id, kind, `location_id`, `sent_at`, transport, outcome) IF NOT EXISTS and stamps `user_version` only when the file's version is lower
  - Opening a store whose `user_version` is greater than `SCHEMA_VERSION` raises a StoreVersionError (no traceback path), covered by a test that stamps `user_version`=99 on a tmp db
  - `derive_unit`('batteryVoltage', '`motion_sensor`') == 'mV'; a one-off backfill sets unit='mV' on existing NULL-unit batteryVoltage rows inside a transaction, guarded by meta key `units_backfill_v2`, and running it twice changes zero rows the second time
  - Store gains `get_health`/`set_health`/`list_health`, `record_transition`/`list_transitions`(`location_id`, since), `record_notification`/`list_notifications`, and `mark_transition_notified`; each has a test
  - Store.prune with the default window deletes readings older than 730 days and leaves health, transitions, and notifications rows untouched (test)
  - A test asserts that for tests/`_fixtures_fleet.py` every measurements key except time lands in readings with the fixture's timestamp, including batteryVoltage with unit mV

### t2 — Health evaluation engine (pure): thresholds, collector state, parent-down suppression, debounce, first-run seeding

- instruction: Files: sensibo/health/`__init__.py`, sensibo/health/model.py, sensibo/health/evaluate.py, tests/`test_health_evaluate.py`. Pure stdlib, no store or network imports; the collector (t5) wires it to the store. Threshold config is the single source of truth consumed later by rooms.py, web, MCP (t9).
- covers: c4, h3, c24, c25, h21, c26, h22
- acceptance:
  - New package sensibo/health with model.py (HealthConfig: default `down_after_seconds`=900 documented as measured from the 91s cadence probe on 2026-09-02, `recovery_hold_cycles`=2, `cooldown_seconds`, `daily_cap`) and evaluate.py (pure function: previous health map + current observations + collector outcome + now -> new health map + transitions + notifications-to-send)
  - A location whose own last reading time is older than `down_after_seconds`, or whose connectionStatus.isAlive is False, transitions to down; readings resuming for `recovery_hold_cycles` consecutive evaluations transitions it back to ok
  - When the collector outcome is a failure (ApiError / no snapshot), every location is set to unknown, no sensor transitions are emitted, and exactly one `collector_unhealthy` notification is emitted; the next successful cycle emits one `collector_recovered`
  - With the parent pod down, its Room Sensors become `unknown_parent_down`, one notification names the pod, and zero child notifications are emitted (fixture with two children)
  - A fixture that flips a sensor between reporting and silent every evaluation yields at most one down and one recovery notification per cooldown window, and the daily cap suppresses further notifications with a suppressed count
  - First evaluation with an empty previous map seeds every location's state and emits one down notification per location already past threshold (decision c38); locations within threshold emit nothing

### t3 — Notification transport: generic webhook POST and script hook, hardened, with redaction and local-execution marker

- instruction: Files: sensibo/notify/`__init__.py`, sensibo/notify/transport.py, sensibo/notify/`_config.py`, tests/`test_notify.py`. Reuse sensibo/api/`_scrub.py` patterns for redaction; do not modify sensibo/api. No new dependency.
- covers: c5, c28, h24, c14, h10
- acceptance:
  - sensibo/notify resolves config from the environment then ~/.sensibo/.env: `SENSIBO_NOTIFY_WEBHOOK` (URL) and/or `SENSIBO_NOTIFY_SCRIPT` (path); with neither set, send() returns a 'not configured' outcome and never raises
  - Webhook delivery is a urllib POST of a JSON payload {kind, location, status, since, `last_ok`, message, execution: 'local (stops when this daemon stops)'} with a 10s timeout; non-2xx and network errors are returned as a failed outcome, not raised
  - Script delivery uses subprocess.run with an argv list, shell=False, a timeout, the JSON payload on stdin, and an environment with `SENSIBO_API_KEY`, `SENSIBO_NOTIFY_WEBHOOK` removed; a test asserts the child env lacks both keys
  - redact() replaces the webhook URL with '<https://…>(redacted)' in every log line, --json payload, and dry-run preview; a test greps rendered output for the URL and finds none
  - `render_dry_run`(payload) returns the exact message text and the redacted transport without sending; a test asserts zero urlopen/subprocess calls in dry-run

### t4 — SVG chart renderer for daily and weekly multi-series reports from the local store

- instruction: Files: sensibo/report/`__init__.py`, sensibo/report/chart.py, tests/`test_report_chart.py`. Read-only against the store. Import helpers from sensibo/web/`_svg.py` if useful but do not edit that file (t9 touches web).
- covers: c7
- acceptance:
  - sensibo/report/chart.py `render_report`(store, `window_hours`, now) returns an SVG string with one panel per (location, numeric field) using Store.`query_range`, downsampled via the same even-downsampling approach as sensibo/web/`_svg.py` to at most 400 points per series
  - Panels carry the location alias or room name, field name and unit, min/max/latest labels, and a title with the window and generation time; an empty series renders a labelled empty panel rather than raising
  - A 7-day window over a fixture store with 3 locations x 5 fields renders in under 2 seconds and the SVG is under 1 MB (test asserts both)
  - The SVG parses as well-formed XML (xml.etree in a test) and contains no script elements

### t5 — Collector integration: persist isAlive, heartbeat, run health evaluation each cycle, persist transitions, dispatch notifications with restart-safe dedupe

- instruction: Files: sensibo/collect/collector.py, sensibo/cli/`_commands`/collect.py, tests/`test_collect.py`, tests/`test_collect_health.py` (new). Use the Store API from t1, evaluate from t2, transport from t3. Do not touch query/web/mcp/rules.
- depends on: t1, t2, t3
- covers: c9, h6, c27, h23, c31, h16, h4
- acceptance:
  - After every cycle Collector stores connectionStatus.isAlive per location as a health observation (not a reading), writes meta `last_cycle_at` and `last_cycle_outcome`, and calls health.evaluate with the previous map from the store
  - Each emitted transition is persisted before its notification is sent; `notified_at` is set only after a successful send; on startup previous health is read from the store, so a test that stops and restarts a Collector mid-outage sends no second down alert and still sends the recovery once
  - An ApiError during a daemon cycle no longer exits the daemon: the cycle records outcome=failed, the health engine sees a collector failure, one `collector_unhealthy` notification is sent, and the loop continues with the normal interval; a --once run still exits 2 on ApiError
  - A test with a client raising ApiError for three cycles then succeeding yields one `collector_unhealthy` and one `collector_recovered` notification and zero sensor-down transitions
  - CycleResult gains health counts (ok, down, unknown, `notifications_sent`, `notifications_suppressed`) and the collect stdout summary and --json show them

### t6 — CLI verbs: query health (offline), notify test (dry-run/--apply), doctor heartbeat, explain catalog and learn entries

- instruction: Files: sensibo/cli/`_commands`/query.py (add health verb), sensibo/cli/`_commands`/notify.py (new), sensibo/cli/`_commands`/doctor.py, sensibo/cli/`__init__.py` (register), sensibo/explain/catalog.py, sensibo/cli/`_commands`/learn.py, tests/`test_cli_health.py`, tests/`test_cli_notify.py`, tests/`test_cli.py`. Do not edit collector or store.
- depends on: t1, t2, t3
- covers: c13, h8, c18, h13, c19, h14, h26
- acceptance:
  - sensibo query health \[LOCATION\] \[--since ISO\] \[--json\] answers from the store only (socket-blocking guard test like `test_query.py`): current status, since, `last_ok`, and the transitions list with computed duration for each closed outage; the spare-room fixture outage renders start, end, and duration
  - sensibo notify test \[--apply\] prints the exact redacted payload and transport without sending, and sends exactly once with --apply; a test asserts no urlopen/subprocess call without --apply
  - sensibo doctor gains a `collector_heartbeat` check reporting meta `last_cycle_at` and outcome, healthy iff within 3x the configured interval; sensibo query health --json includes collector: {`last_cycle_at`, outcome}
  - explain catalog has entries for ('query','health'), ('notify',), ('notify','test'); learn output mentions health, notify, and the local-execution marker; uv run teken cli doctor . --strict passes
  - Every notify/health/report output line names execution as 'local (stops when this daemon stops)' (shared constant from sensibo/rules/model.py or a new sensibo/`_execution.py`)

### t7 — Report scheduling and delivery: in-daemon clock with persisted last-sent, files under ~/.sensibo/reports, report verb

- instruction: Files: sensibo/report/schedule.py, sensibo/report/deliver.py, sensibo/cli/`_commands`/report.py (new), sensibo/cli/`_commands`/collect.py (one hook call after the health step), sensibo/explain/catalog.py (report entries), tests/`test_report_schedule.py`, tests/`test_cli_report.py`. Depends on t5 having landed so the collect.py edit is sequential, not a same-wave conflict.
- depends on: t4, t5
- covers: h5, c29, h25
- acceptance:
  - sensibo/report/schedule.py decides, from meta `last_daily_report_at` / `last_weekly_report_at` and a host-local now, whether a daily (07:00 local) or weekly (Monday 07:00 local) report is due; both hours are configurable via `SENSIBO_REPORT_DAILY_AT` and `SENSIBO_REPORT_WEEKLY_AT`
  - A clock-controlled test restarting the scheduler at 06:59 and 07:01 yields exactly one daily report; a daemon down across two due instants sends at most one catch-up report
  - Reports are written to ~/.sensibo/reports/daily-YYYY-MM-DD.svg and weekly-YYYY-Www.svg (directory mode 0700), and the notification payload kind=report carries the file path and the dashboard URL when the web server's bind is known; no multipart upload is performed
  - sensibo report daily|weekly \[--out PATH\] \[--apply\] renders to stdout/path without sending by default and delivers via the configured hook with --apply; explain catalog entry added; teken rubric passes
  - The collect daemon loop calls the scheduler after each cycle; a test with a fake clock shows a report generated and delivered from the loop

### t8 — Rules: the stale leaf condition, reading persisted health, with dry-run visibility and untouched safety gates

- instruction: Files: sensibo/rules/model.py, sensibo/rules/evaluate.py, tests/`test_rules_engine.py`, examples/stale-room.rule.json, docs for rules (the rules section only). No engine.py or persistence.py edits.
- depends on: t1, t2
- covers: c37, h19, c16, h20, c20, h15
- acceptance:
  - `LEAF_TYPES` gains 'stale'; {type: stale, location: LOCATION, `after_seconds`: optional} validates (`after_seconds` positive int if present) and rejects unknown keys with RuleValidationError
  - Evaluation reads the location's persisted health from the store: true when status is down or unknown (or `last_ok` older than `after_seconds` when given), false when ok; a test drives a location through reporting -> silent -> reporting and asserts true then false
  - Rule dry-run output lists the stale leaf's evaluation with the location's status and `last_ok` like other leaves
  - git diff of this task against sensibo/rules/ touches only leaf validation/dispatch and tests; the hysteresis, minimum-off-time, and rate-limit code and their tests are byte-identical (a test file checksum recorded in the PR description)
  - examples/ gains a rule using stale to stop trusting a dead room's motion; docs for rules mention the leaf

### t9 — One staleness source of truth across room list, web dashboard, and MCP; serve reports from the dashboard

- instruction: Files: sensibo/store/rooms.py, sensibo/web/`_wire.py`, sensibo/web/`_render.py`, sensibo/web/server.py, sensibo/`mcp_server`/`_tools.py`, tests/`test_room.py`, tests/`test_web_server.py`, tests/`test_mcp_tools.py`, docs/web.md, docs/mcp.md. Do not touch collector, store schema, or CLI command modules.
- depends on: t1, t2
- covers: c32, h27
- acceptance:
  - `DEFAULT_STALE_AFTER_HOURS` is replaced by the health config threshold; `is_stale` and every caller (room list, web `_wire`/`_render`, MCP locations tool) derive STALE from the same HealthConfig; a test asserts the three surfaces agree for a location just past threshold
  - The dashboard and the MCP locations tool show status (ok/down/unknown), since, and `last_ok` from the health table, plus the collector heartbeat; a new MCP tool `sensibo_health` mirrors sensibo query health
  - The web server serves ~/.sensibo/reports/\*.svg at /reports/ (open reads, same trust model as the dashboard) and lists them on the index; path traversal is rejected with 404 (test)
  - Existing web, MCP, and room tests pass with updated expectations; no change to the token-gated POST handlers

### t10 — Docs: health guide, API doc corrections, outage evidence, README/CLAUDE.md/roadmap/deployment updates, learn sync

- instruction: Files: docs/\*.md, README.md, CLAUDE.md, .claude/skills/manage-ac/SKILL.md (quick-reference rows for query health, notify test, report). Docs only; no code.
- depends on: t5, t6, t7, t8, t9
- covers: c12, h9, c35, h18
- acceptance:
  - docs/sensibo-api.md Room Sensor row says batteryVoltage (mV, quantized: observed flat at 3000 then cliff) instead of battery, and records the 91s uniform cadence probe
  - docs/health.md documents the three outage classes (single sensor, all children of one pod, all locations at once) with the reproducing SQL against the live store for the 2026-02-10 and 2026-08-02 outages, the health states, thresholds, debounce, notification config (webhook/script), redaction, and the local-execution caveat
  - README, CLAUDE.md ('current state' no longer says scaffold), docs/roadmap.md (health answered; heartbeat), docs/deployment.md (no new units; reports directory) are updated; markdownlint passes; `test_learn_sync` passes

### t11 — Real-fleet acceptance run: battery pull on a Room Sensor, evidence filed via validate-delivery

- instruction: Manual, operator-performed. Not automatable in CI. File outcomes verbatim with devague evidence (pass or fail), per the validate-delivery skill.
- depends on: t5, t6, t7
- covers: c1, h1, c17, h12
- acceptance:
  - With the new daemon running as sensibo-collect.service, the operator removes a Room Sensor battery; within 3 poll cycles sensibo query health shows it down with a since timestamp and one notification arrives via the configured hook naming the sensor and its last-heard time
  - Re-inserting the battery produces exactly one recovery notification and query health shows the closed outage with its duration
  - The next daily report arrives at the configured hour with one panel per location and field; the run's timestamps, notification payloads (redacted), and query output are filed as devague evidence records on this plan

## Risks

- [unknown_nonblocking] Mixed-version upgrade window: the running sensibo-collect.service (v1 store code) keeps calling `init_schema` while a new binary migrates to v2; t1's version guard must tolerate the old daemon until systemctl --user restart, and the upgrade docs must say restart order (task t1)
- [unknown_nonblocking] Two writers on one sqlite file: the collect daemon plus a manual notify test --apply or report --apply writing notifications rows; WAL and the default 5s busy timeout are assumed sufficient, untested under load (task t5)
- [unknown_nonblocking] Changing the daemon loop to survive ApiError alters the systemd Restart=always semantics: a permanently bad key now produces a notification per cooldown instead of a crash loop; the doctor check and docs must make this visible (task t5)
- [follow_up] batteryVoltage is quantized (flat 3000 for 14 days); a low-voltage warning may never fire before death, so the down alert is the real signal and the warning threshold stays provisional (task t2)
- [unknown_nonblocking] The real-fleet acceptance (t11) needs the operator to physically pull a battery; it cannot run in CI and its evidence is filed manually (task t11)
