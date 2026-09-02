# sensor health, alerts, and chart reports

> sensibo-cli now watches every sensor in the home: each reading every pod and Room Sensor reports lands in the local sqlite store, a sensor that goes quiet or runs its battery down is flagged and announced on Discord, and a daily and a weekly chart of the measurements is delivered as an image report
> instruction: Acceptance: pull a Room Sensor battery on the real fleet; within 3 poll cycles sensibo query health shows it down with a since timestamp, one notification arrives via the configured hook, and re-inserting the battery produces one recovery notification and closes the transition

## Audience

- The home operator, who wants to hear about a dead sensor without checking a dashboard, and the agents (CLI, MCP) that read the store and should be able to ask 'which sensors are down right now'

## Before → After

- Before: The spare-room Room Sensor (`ms_o7dH4GeY`) was silent from 2026-02-10 05:52 until 2026-09-02 07:49 - about seven months - and nothing told the operator; the bedroom sensor had 9 gaps longer than 6h in July 2026 alone. The web dashboard shows a STALE flag only to whoever happens to look
- Before: Both Room Sensors were down at once and came back together: the bedroom sensor (`ms_kDup7cVx`) was silent 2026-08-02 23:00 to 2026-09-02 07:51 (about 30 days) and the spare-room sensor since 2026-02-10, and both resumed within two minutes of each other this morning - a parent-pod or BLE-side cause is at least as likely as two battery deaths, and the pod itself had 12 gaps over 30 minutes in the last 60 days, several shared with the bedroom sensor to the minute (collector or cloud outages). Nothing distinguished these cases and nothing notified
- After: The operator no longer discovers a dead sensor by noticing a gap months later: a sensor that goes quiet or runs low on battery is announced within a few poll cycles, its outage is recorded with a start and end in the store, every reading it ever sent is still queryable, and a daily and weekly chart arrive without anyone opening the dashboard

## Why it matters

- Rules and comfort decisions are made from these readings; a room whose sensor died in February silently feeds seven-month-old motion and temperature into every decision until someone happens to look. Retention is only worth something if the operator knows the data is still flowing

## Requirements

- Every number a sensor provides is saved: temperature, humidity, movement, air quality, battery - whatever fields each pod and Room Sensor reports - into a queryable local database
  - instruction: Verify against the live store: compare the field list from one fleet snapshot with SELECT DISTINCT field FROM readings per location; add batteryVoltage -> mV to sensibo/store/`_units.py` and fix the Room Sensor row in docs/sensibo-api.md
  - honesty: For every location in sensibo devices --json, every key of its measurements object except time has rows in the readings table with the same timestamps; batteryVoltage carries a unit
- The system detects when a sensor is down, e.g. its battery ran out, and records that state
  - instruction: Add a health table (`location_id`, status, since, `last_ok`) plus a transitions log to sensibo/store; evaluate after each collect cycle; expose it as sensibo query health (offline, --json) and in the MCP locations tool
  - honesty: Down is derived from the location's own last reading time and connectionStatus with a per-kind threshold far below 24h, and each up/down transition is persisted with its timestamps so an outage survives a daemon restart
- When a device is down a notification goes out - to Discord in the operator's case - through either a generic webhook URL or an operator-configured script that the daemon runs; there is no dependency on the discord-bot-cli, which would be too specific
  - instruction: Config in ~/.sensibo/.env or a notify section: `SENSIBO_NOTIFY_WEBHOOK` (POST JSON) or `SENSIBO_NOTIFY_SCRIPT` (argv + JSON on stdin); verb sensibo notify test dry-runs by default and sends with --apply; scrub the webhook URL from logs like the API key
  - honesty: With only a webhook URL or a script path configured, a down transition produces exactly one outbound notification and a recovery produces exactly one; the transport is stdlib urllib or subprocess, no new dependency
- A daily and a weekly image report - a diagram of the collected measurements - is produced and delivered
  - instruction: Build on sensibo/web/`_svg.py` `render_sparkline`: a multi-series chart over `query_range` for 24h and 7d; verb sensibo report daily|weekly \[--out PATH\] \[--apply to deliver\]; scheduling per q3
  - honesty: A daily and a weekly report render one chart per location and field from the local store alone, in the image format the user decides (q1), and are delivered through the same webhook/script hook as alerts
- Liveness is persisted, not only derived: the collector drops connectionStatus.isAlive today (sensibo/collect/collector.py records only measurements fields), so a health record per location - status, `down_since`, `last_ok`, and each up/down transition - is added to the store so 'when did it go down and for how long' is answerable offline
  - honesty: connectionStatus.isAlive is stored on every cycle as a health fact, not as a reading, and a location's status can be reconstructed for any past instant from the transitions log
- Battery is tracked by its real field name: the operator's Room Sensors report batteryVoltage in millivolts (3000 fresh; 1638 and 1728 were the last values before each sensor went silent), while sensibo/store/`_units.py` only knows 'battery' as % and docs/sensibo-api.md lists the field as 'battery' - both are corrected and a low-voltage warning threshold is added
  - honesty: The unit for batteryVoltage is recorded as mV for new rows, existing NULL-unit rows are backfilled by a one-off migration, and a low-voltage warning fires below a configurable threshold whose default is marked provisional
- Any verb that sends a notification or a report is dry-run by default and commits with --apply, like every other write verb in this repo (CLAUDE.md conventions), and it gets an explain catalog entry or the teken rubric gate fails
  - honesty: Every new write verb without --apply changes nothing and prints what it would send; each new verb has an explain catalog entry and teken cli doctor --strict stays green
- Collector health is a state distinct from sensor health: when the fleet snapshot fails (cloud unreachable, 401, exhausted 429 retries) or no cycle has run within the threshold, every location is marked unknown rather than down and exactly one 'collector unhealthy' notification fires instead of one alert per sensor. Provenance: sensibo/cli/`_commands`/collect.py's daemon loop exits on ApiError and systemd restarts it, so a cloud outage stops all readings at once; in the live store the largest gap in the last 14 days (1034s) hit all three locations at the same instant
  - honesty: Simulating a cloud outage in tests (client raising ApiError for several cycles) yields one collector notification and zero sensor-down transitions; readings resuming clears it with one recovery
- Room Sensors are BLE satellites of a parent pod (docs/sensibo-api.md Trap 2; locations.`parent_pod_id` in the schema): when the parent pod is offline its Room Sensors are marked unknown-parent-down, one alert names the pod, and no child alerts fire; a child going quiet while its parent is alive is the genuine sensor-down case
  - honesty: With the parent pod offline in a fixture, its two Room Sensors are marked unknown-parent-down, one notification names the pod, and zero child notifications are sent
- Alerts are debounced: a location must be past its threshold for a full evaluation and a recovery must hold for at least 2 consecutive cycles before notifying; a per-location cooldown and a daily cap on notifications prevent a flapping sensor from producing dozens of messages. Provenance: the bedroom sensor had 9 gaps over 6h in July 2026 alone
  - honesty: A fixture that flips a sensor between reporting and silent every cycle produces at most one down and one recovery notification per cooldown window, and the daily cap is enforced in a test
- Notification state is persisted, never in-memory: each transition row carries `notified_at`, and on startup the daemon re-derives current health from the store, so a daemon restart mid-outage cannot re-send a down alert or lose a pending recovery alert - this is what makes honesty condition h4's 'exactly one' true across systemd restarts
  - honesty: Killing and restarting the daemon mid-outage in a test sends no second down alert, and the pending recovery still fires once when readings resume
- The script hook runs as an argv list with no shell, a timeout, and a child environment with `SENSIBO_API_KEY` and the webhook URL removed; the webhook URL is a secret (a Discord webhook URL grants post rights) and is scrubbed from logs, --json output, and the dry-run preview exactly like the API key (sensibo/api/`_scrub.py`)
  - honesty: The script is invoked with subprocess and an argv list, shell=False, a timeout, and an env lacking `SENSIBO_API_KEY` and the webhook URL; the dry-run preview and --json output show the URL redacted, verified by tests
- Report scheduling survives restarts: `last_daily_report_at` and `last_weekly_report_at` live in the store meta table, a restart sends at most one missed report per kind, and never a duplicate
  - honesty: Restarting the daemon just before and just after the report hour in a clock-controlled test yields exactly one daily report
- Schema change is a guarded migration: new health, transitions, and notifications tables are created IF NOT EXISTS, `SCHEMA_VERSION` goes 1 to 2, a binary that finds a newer `user_version` than it knows fails closed with a remediation instead of silently restamping it, and the batteryVoltage NULL-unit backfill is one idempotent UPDATE in a transaction guarded by a meta flag. Provenance: sensibo/store/`_schema.py` `init_schema` stamps PRAGMA `user_version` = `SCHEMA_VERSION` unconditionally on every connect, so today an old binary would downgrade a v2 store's version stamp
  - honesty: Opening a v2 store with a v1-era `init_schema` is covered by a test that expects a CliError with a hint, and running the backfill twice changes zero rows the second time
- The collector records a heartbeat (`last_cycle_at`, last cycle outcome) in store meta after every cycle; sensibo query health and sensibo doctor show it, and an opt-in daily heartbeat notification tells the operator the watcher itself is alive. Provenance: docs/roadmap.md already requires 'the daemon must be able to tell you when it last ran' and nothing implements it
  - honesty: After every cycle meta.`last_cycle_at` is updated; sensibo query health --json and sensibo doctor both show it, and the opt-in heartbeat notification fires once per day when enabled
- Staleness has one source of truth: the per-kind thresholds that drive alerts also drive the STALE flag in room list, the web dashboard, and the MCP locations tool, replacing the 24h `DEFAULT_STALE_AFTER_HOURS` default so a sensor never reads fresh on the dashboard while an alert has fired
  - honesty: room list, the dashboard, and the MCP locations tool derive STALE from the same threshold config the alerter uses; a test asserts they agree for a location just past threshold
- The rules grammar gains exactly one leaf: {type: stale, location: LOCATION, `after_seconds`: optional} - true when the named location's persisted health is down or unknown (default threshold from the health config); rule dry-run output shows the stale evaluation like any other leaf, and the compressor safety gates are unchanged
  - honesty: A rule with a stale leaf on a location that stopped reporting evaluates true in dry-run within the threshold, false again after recovery, and tests/`test_rules_engine.py` covers both; the hysteresis and rate-limit tests are unchanged

## Honesty conditions

- A real battery pull on a Room Sensor produces a stored down transition and a delivered notification without any manual step, and the same store answers 'how long was it down' afterwards
- The outage dates are reproducible from the live store: SELECT min/max timestamp of `ms_o7dH4GeY` readings shows the 2026-02-10 to 2026-09-02 gap, and no notification code path existed in the repo at tag 0.7.2
- Every notification and report output carries the same local-execution marker rules use, and stopping sensibo-collect.service stops alerts and reports with no cloud fallback claimed anywhere in docs or learn output
- git diff of the implementing PRs against sensibo/rules/ touches only the leaf dispatch and validation for the new stale type plus its tests; hysteresis, min-off-time, and rate-limit code and tests are byte-identical
- Tested on the real fleet by pulling a Room Sensor battery: one down notification within 3 poll cycles naming the sensor and its last-heard time, one recovery notification on re-insert, and the daily SVG arrives on schedule with one series per location and field
- The operator receives alerts without opening the dashboard, and an agent can ask 'which sensors are down right now' offline via sensibo query health --json and the MCP locations tool
- For a real outage the store answers when it started, when it ended, and how long it lasted, all readings before and after remain queryable, and the alert arrived within a few poll cycles of the start
- A dead sensor's staleness is visible to rule evaluation and to the operator so that no rule silently acts on months-old motion or temperature; the spare-room case is the regression test
- Store.prune with the default window deletes readings older than 730 days and leaves the health, transitions, and notifications tables untouched, asserted by a test
- The default threshold is a named constant documented as measured (91s cadence probe, 2026-09-02) and overridable in config; alerts fire only after it elapses
- The three outage classes - single sensor, all children of one pod, all locations at once - are each reproducible from the live store's gap history and each maps to a distinct health state in the delivered design

## Success signals

- Pulling a Room Sensor's battery produces a Discord message within a few poll cycles naming the sensor and when it was last heard; re-inserting it produces a recovery message; the daily image lands every morning with one series per location and field

## Scope / boundaries

- Alerts and reports run on this machine and stop when the local daemon stops, exactly like local rules (sensibo/rules/model.py `EXECUTION_LOCAL`, docs/roadmap.md 'every rule states where it runs'); their output says so, and a dead collector daemon produces no alert - that watchdog gap is named, not hidden
- The rules engine changes in exactly one way: a new 'stale' leaf type. The existing threshold/occupancy/`time_window` semantics, the hysteresis and minimum-off-time gates, and the rate limiter are not modified
- Store.prune (default 730 days) keeps deleting readings only; health transitions and notification records are not pruned by it and keep their own, longer retention so an outage history outlives the raw readings it was derived from

## Non-goals

- No runtime dependency is added for charts or notifications: pyproject.toml keeps dependencies = \[\], sensibo/web/`_svg.py` already renders stdlib-only SVG sparklines to build on, and a notification leaves the machine either as a plain urllib POST to a generic webhook or by running an operator-configured script

## Assumptions

- The 'save every reading in sqlite' half already shipped and is not rebuilt: sensibo/store keeps a field-flexible readings table (`location_id`, field, timestamp) and sensibo/collect stores whatever keys each measurements object carries; the operator's real store holds 413,092 rows across 3 locations since 2026-02-10 and the sensibo-collect systemd unit is active - the delta for this idea is health, alerting, and reports on top of that store
- 'Down' is decided from the location's own last reading time going stale (the existing `is_stale` helper in sensibo/store/rooms.py, default 24h, already used by room list, the web dashboard, and the MCP locations tool) combined with connectionStatus, with the threshold configurable per kind - a Room Sensor reports every ~90s so 24h is far too slow for a battery-out alert
- Down threshold default is about 15 minutes (10 missed 91s cycles) for pods and Room Sensors alike: a read-only probe of the live store over the last 14 days shows p50 = p90 = 91s and p99 = 132s inter-reading intervals for all three locations, so cadence does not differ by kind

## Scope exploration

- `s1` — `sensibo/store/_schema.py + sensibo/store/store.py`: the retention pillar exists: EAV readings table keyed (`location_id`, field, timestamp) with upsert semantics, a locations table with `first_seen`/`last_seen`, and a meta side-table; there is no health/status table and no record of up/down transitions - `last_seen` is the only liveness fact and it is overwritten, never historised
  - seeds: `c8`, `c9`, `c2`, `c3`
- `s2` — `sensibo/collect/collector.py`: `run_cycle` stores only keys of each measurements object (plus room sensors nested in motionSensors); connectionStatus.isAlive is read by the devices/read verbs (`_fleet.py`) but never persisted; `_seen_at` deliberately uses the reading's own time so a dead sensor's `last_seen` freezes - the right raw signal for staleness is already there
  - seeds: `c9`, `c10`
- `s3` — `sensibo/store/_units.py + docs/sensibo-api.md per-model table`: the unit map has 'battery' -> '%' but the operator's `motion_sensor` devices report 'batteryVoltage' (unit stored NULL); the API doc's Room Sensor row also says 'battery' - a doc-vs-fleet mismatch caught against real hardware
  - seeds: `c11`
- `s4` — `~/.sensibo/sensibo.db (live store) + sensibo devices --json (one read-only fleet call)`: fleet is 1 airq pod (8DdxNuyc, Central) and 2 `motion_sensor` Room Sensors (`ms_kDup7cVx` bedroom, `ms_o7dH4GeY` spare-room); pod fields co2 etoh feelsLike humidity iaq motion roomIsOccupied rssi temperature tvoc, sensor fields batteryVoltage humidity motion rssi temperature; spare-room silent 2026-02-10 -> 2026-09-02 07:49 (320 rows vs bedroom's 7958), bedroom 9 gaps >6h in July; batteryVoltage 3000 fresh, 1638/1728 last-before-silence
  - seeds: `c12`, `c11`, `c10`
- `s5` — `sensibo/store/rooms.py is_stale + sensibo/web/_wire.py + sensibo/web/_render.py + sensibo/mcp_server/_tools.py`: staleness (default 24h) is computed at read time and surfaced as a STALE flag in room list, the dashboard, and the MCP locations tool; nothing persists it, nothing notifies, and 24h is tuned for a human glance not for a ~90s-cadence sensor
  - seeds: `c10`, `c9`
- `s6` — `sensibo/rules/model.py + sensibo/rules/evaluate.py`: leaf condition types are threshold, occupancy, `time_window` - there is no 'location is stale/down' condition, and rules only drive acState, never a notification; the engine is explicitly local-execution and carries the `EXECUTION_LOCAL` marker every output renders
  - seeds: `c16`, `c14`
- `s7` — `sensibo/web/_svg.py`: `render_sparkline` draws stdlib-only SVG with even downsampling (`max_points`) from ReadingRecord lists - a ready base for a daily/weekly multi-series chart; there is no rasteriser, and PNG output is impossible without a new dependency or an external tool
  - seeds: `c15`, `c7`
- `s8` — `pyproject.toml dependencies = [] + CLAUDE.md 'Zero runtime dependencies, deliberately'`: adding a chart or notification library is a recorded architectural decision, not a convenience; a plain urllib POST covers a generic webhook and subprocess covers an operator-configured script hook without any new dependency
  - seeds: `c15`, `c5`
- `s9` — `../discord-bot-cli (sibling repo) + discord binary on PATH`: a separate agent CLI with channel/message/thread/user verbs exists in the workspace, but the user ruled it out as too specific a dependency; this repo has zero references to Discord today, so the notification surface is a generic webhook POST or a configurable script hook
  - seeds: `c5`, `c6`, `c15`
- `s10` — `sensibo/service/_units.py + docs/deployment.md + docs/roadmap.md 'Answered: the always-on host'`: service install writes user units for collect --daemon and web only, Restart=always, lingering enabled; the rules daemon was deliberately excluded; report/alert scheduling has no unit or timer yet and would need either an in-daemon scheduler or a systemd timer
  - seeds: `c14`
- `s11` — `sensibo/explain/catalog.py + sensibo/cli/_commands/ + CLAUDE.md conventions`: a new verb is a module with register(), wired in `_build_parser`(), plus a catalog entry (teken cli doctor --strict fails otherwise); every write verb is dry-run by default with --apply; results to stdout, diagnostics to stderr, --json everywhere
  - seeds: `c13`
- `s12` — `docs/specs/2026-07-14-...md + .devague/frames (previous exported frame)`: the shipped spec already named the stale sensor ('last reported 2026-02-10 - likely offline or battery-dead') and tests/`_fixtures_fleet.py` models one stale Room Sensor with connectionStatus.isAlive false and empty measurements - the problem was seen, only the alerting was never scoped
  - seeds: `c12`
- `s13` — `challenge pass / failure-mode lens: sensibo/cli/_commands/collect.py daemon loop + sensibo/api/client.py 429 backoff + live store 14-day gap probe`: an ApiError ends the daemon (exit 2, systemd Restart=always); there is no per-cycle failure record, so during a cloud or key outage every location would look stale simultaneously - the 1034s gap common to all three locations today is exactly that signature
  - seeds: `c24`
- `s14` — `challenge pass / adjacent-systems lens: sensibo/store/_schema.py parent_pod_id + docs/sensibo-api.md Trap 2`: the hierarchy is already modelled but nothing in the exported spec uses it for health; without it a pod outage fans out into N alerts
  - seeds: `c25`
- `s15` — `challenge pass / failure-mode lens: live store gap history for ms_kDup7cVx`: flapping is real on this fleet, so an un-debounced alert path would have produced 18 messages in one month from one sensor
  - seeds: `c26`
- `s16` — `challenge pass / lifecycle lens: systemd Restart=always in sensibo/service + h4 'exactly one notification'`: h4 is only satisfiable if dedupe state lives in sqlite; the spec's health table instruction on c4 did not say where notified state lives
  - seeds: `c27`
- `s17` — `challenge pass / security lens: sensibo/api/_scrub.py + sensibo/api/_auth.py (~/.sensibo/.env, chmod 600) + the c5 instruction`: the c5 instruction says scrub the URL from logs but the dry-run preview prints 'what it would send', which would leak the URL to a terminal or an agent transcript; the child process inherits the API key unless stripped
  - seeds: `c28`
- `s18` — `challenge pass / lifecycle lens: sensibo/store/_schema.py meta table + collect daemon loop`: the loop has no clock state; with in-daemon scheduling (decision c23) a restart at 06:59 and 07:01 would double-send or skip unless the last-sent instant is persisted
  - seeds: `c29`
- `s19` — `challenge pass / migration lens: sensibo/store/_schema.py init_schema + pragma user_version=1 on the live store`: every process (collect, web, mcp, query) calls `init_schema` on connect; the unconditional version stamp is the concrete downgrade hazard for a mixed-version window during upgrade
  - seeds: `c30`
- `s20` — `challenge pass / observability lens: docs/roadmap.md 'daemon must tell you when it last ran' + doctor command`: an unmet standing requirement that also bounds the parked 'who watches the watcher' unknown
  - seeds: `c31`
- `s21` — `challenge pass / adjacent-systems lens: sensibo/store/rooms.py DEFAULT_STALE_AFTER_HOURS + sensibo/web/_wire.py + sensibo/mcp_server/_tools.py`: three read surfaces hardcode the 24h default via parameter defaults; leaving them makes dashboard and alerts disagree
  - seeds: `c32`
- `s22` — `challenge pass / data-flow lens: sensibo/store/store.py prune + DELETE_OLDER_THAN_SQL`: prune targets the readings table by timestamp; new tables need an explicit retention decision or they are implicitly forever
  - seeds: `c33`
- `s23` — `challenge pass / cheap-probe lens: ~/.sensibo/sensibo.db inter-reading intervals, last 14 days, read-only`: cadence is uniform at 91s across pod and Room Sensors (n=13363 pod, 351/353 sensors); this replaces the per-kind-threshold guess in c10 with a measured default
  - seeds: `c34`
- `s24` — `challenge pass / concurrency lens: sensibo/store/store.py connect (WAL, default timeout) + sensibo/web/server.py per-request Store() + sensibo/mcp_server/_tools.py`: every reader opens its own connection per request; WAL isolates readers from the single daemon writer; a second writer is the untested case (parked)
- `s25` — `challenge pass / counter-evidence lens: ~/.sensibo/sensibo.db gap history >30min, last 60 days, all locations (read-only)`: bedroom sensor down 30 days (08-02 to 09-02), both sensors recovered simultaneously at 07:49-07:51, pod gaps shared to the minute with the bedroom sensor; corrects s4, which called the bedroom sensor flapping because only the first six of its gaps were printed
  - seeds: `c35`, `c25`, `c24`

## Decisions

- The store is sqlite (or something equally small and file-based); no database server
- Notification transport is a generic webhook (plain HTTP POST) or a configurable script hook; not a Discord-specific client or library
- Report image format is SVG, produced by the existing stdlib renderer; no rasteriser and no new dependency. Discord will not preview it inline; that is accepted
- Sensor-down is both a notification concern and a rule condition: a 'stale' leaf joins the rules grammar (decision c36, made after the challenge pass); the compressor safety gates are untouched
- Health evaluation and report scheduling run inside the collect daemon loop: health after every poll cycle, reports on a clock in the same process; no new systemd units
- Sensor-down becomes a proper rule condition: a new 'stale' leaf type in the rules grammar, true when a location's health is down or unknown; this supersedes the earlier notification-only decision (c22) after the challenge pass surfaced the h15 contradiction
- First run after upgrade: initial health evaluation seeds state and sends one alert per location already past its threshold, so existing outages are announced immediately
- Reports are written to ~/.sensibo/reports/ as SVG, served by the web dashboard, and the notification payload carries the file path and dashboard URL; the script hook may upload the file itself. No multipart upload from sensibo
- Report clock: daily at 07:00 host-local, weekly Monday 07:00 host-local, both configurable

## Hard questions

- risk: If the collect daemon dies, no alert fires; a heartbeat notification (daily 'still alive' or a report that simply arrives) is the cheapest watchdog and is worth deciding on
- contradiction with c23? (resolved: filed against the wrong id (c23 instead of c22); superseded by q3 and decision c36)
- h15 (on c20) promises staleness is visible to rule evaluation so no rule acts on dead data, but decision c22 says sensor-down does not become a rule condition and the rules grammar is untouched; which holds - or is the middle path that rule evaluation treats a stale location's readings as missing (condition false) without any new grammar? (resolved: user chose to add a proper rule condition (stale leaf); c22 and c16 amended, h11 rejected, c37 captured)

## Open parks

- [unknown_nonblocking] The real battery-dead voltage threshold: the two observed last-before-silence values are 1638 mV and 1728 mV, one sensor each - not enough to fix a warning level; needs more outages or Sensibo documentation
- [unknown_nonblocking] Whether Sensibo flips connectionStatus.isAlive to false for a battery-dead Room Sensor or only freezes its measurements - today both sensors read online after recovery, the fixture assumes false, and the store never recorded the value during the 7-month outage
- [unknown_nonblocking] Who watches the watcher: if the collect daemon itself dies no down-alert can fire; systemd Restart=always covers crashes but not a powered-off host
- [unknown_nonblocking] batteryVoltage is quantized: all 352 bedroom readings in the last 14 days are exactly 3000 mV, so the value may sit flat and then cliff, and a low-voltage warning threshold could never trigger before the sensor dies - the warning may need to be 'voltage dropped below 3000 at all' plus the down alert as the real signal
- [unknown_nonblocking] Two writers on one sqlite file: the collect daemon, the web server's POST handlers, MCP, and a manual 'notify test --apply' all open the store with sqlite3.connect's default 5s busy timeout and WAL; whether a manual write can collide with a daemon cycle under load is untested
