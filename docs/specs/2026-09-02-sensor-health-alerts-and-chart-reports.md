# sensor health, alerts, and chart reports

> sensibo-cli now watches every sensor in the home: each reading every pod and Room Sensor reports lands in the local sqlite store, a sensor that goes quiet or runs its battery down is flagged and announced on Discord, and a daily and a weekly chart of the measurements is delivered as an image report
> instruction: Acceptance: pull a Room Sensor battery on the real fleet; within 3 poll cycles sensibo query health shows it down with a since timestamp, one notification arrives via the configured hook, and re-inserting the battery produces one recovery notification and closes the transition

## Audience

- The home operator, who wants to hear about a dead sensor without checking a dashboard, and the agents (CLI, MCP) that read the store and should be able to ask 'which sensors are down right now'

## Before → After

- Before: The spare-room Room Sensor (`ms_o7dH4GeY`) was silent from 2026-02-10 05:52 until 2026-09-02 07:49 - about seven months - and nothing told the operator; the bedroom sensor had 9 gaps longer than 6h in July 2026 alone. The web dashboard shows a STALE flag only to whoever happens to look
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

## Honesty conditions

- A real battery pull on a Room Sensor produces a stored down transition and a delivered notification without any manual step, and the same store answers 'how long was it down' afterwards
- The outage dates are reproducible from the live store: SELECT min/max timestamp of `ms_o7dH4GeY` readings shows the 2026-02-10 to 2026-09-02 gap, and no notification code path existed in the repo at tag 0.7.2
- Every notification and report output carries the same local-execution marker rules use, and stopping sensibo-collect.service stops alerts and reports with no cloud fallback claimed anywhere in docs or learn output
- sensibo/rules/model.py `LEAF_TYPES`, the hysteresis and rate-limit code, and tests/`test_rules_engine.py` are unchanged by the implementing PRs (git diff --stat against them is empty)
- Tested on the real fleet by pulling a Room Sensor battery: one down notification within 3 poll cycles naming the sensor and its last-heard time, one recovery notification on re-insert, and the daily SVG arrives on schedule with one series per location and field
- The operator receives alerts without opening the dashboard, and an agent can ask 'which sensors are down right now' offline via sensibo query health --json and the MCP locations tool
- For a real outage the store answers when it started, when it ended, and how long it lasted, all readings before and after remain queryable, and the alert arrived within a few poll cycles of the start
- A dead sensor's staleness is visible to rule evaluation and to the operator so that no rule silently acts on months-old motion or temperature; the spare-room case is the regression test

## Success signals

- Pulling a Room Sensor's battery produces a Discord message within a few poll cycles naming the sensor and when it was last heard; re-inserting it produces a recovery message; the daily image lands every morning with one series per location and field

## Scope / boundaries

- Alerts and reports run on this machine and stop when the local daemon stops, exactly like local rules (sensibo/rules/model.py `EXECUTION_LOCAL`, docs/roadmap.md 'every rule states where it runs'); their output says so, and a dead collector daemon produces no alert - that watchdog gap is named, not hidden
- The local rules engine's condition grammar (threshold, occupancy, `time_window` in sensibo/rules/model.py) and the compressor safety gates are not changed by this work unless the user decides sensor-down should also be a rule condition

## Non-goals

- No runtime dependency is added for charts or notifications: pyproject.toml keeps dependencies = \[\], sensibo/web/`_svg.py` already renders stdlib-only SVG sparklines to build on, and a notification leaves the machine either as a plain urllib POST to a generic webhook or by running an operator-configured script

## Assumptions

- The 'save every reading in sqlite' half already shipped and is not rebuilt: sensibo/store keeps a field-flexible readings table (`location_id`, field, timestamp) and sensibo/collect stores whatever keys each measurements object carries; the operator's real store holds 413,092 rows across 3 locations since 2026-02-10 and the sensibo-collect systemd unit is active - the delta for this idea is health, alerting, and reports on top of that store
- 'Down' is decided from the location's own last reading time going stale (the existing `is_stale` helper in sensibo/store/rooms.py, default 24h, already used by room list, the web dashboard, and the MCP locations tool) combined with connectionStatus, with the threshold configurable per kind - a Room Sensor reports every ~90s so 24h is far too slow for a battery-out alert

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

## Decisions

- The store is sqlite (or something equally small and file-based); no database server
- Notification transport is a generic webhook (plain HTTP POST) or a configurable script hook; not a Discord-specific client or library
- Report image format is SVG, produced by the existing stdlib renderer; no rasteriser and no new dependency. Discord will not preview it inline; that is accepted
- Sensor-down stays a notification concern in this scope; it does not become a rule condition and the rules grammar is untouched
- Health evaluation and report scheduling run inside the collect daemon loop: health after every poll cycle, reports on a clock in the same process; no new systemd units

## Hard questions

- risk: If the collect daemon dies, no alert fires; a heartbeat notification (daily 'still alive' or a report that simply arrives) is the cheapest watchdog and is worth deciding on

## Open parks

- [unknown_nonblocking] The real battery-dead voltage threshold: the two observed last-before-silence values are 1638 mV and 1728 mV, one sensor each - not enough to fix a warning level; needs more outages or Sensibo documentation
- [unknown_nonblocking] Whether Sensibo flips connectionStatus.isAlive to false for a battery-dead Room Sensor or only freezes its measurements - today both sensors read online after recovery, the fixture assumes false, and the store never recorded the value during the 7-month outage
- [unknown_nonblocking] Who watches the watcher: if the collect daemon itself dies no down-alert can fire; systemd Restart=always covers crashes but not a powered-off host
