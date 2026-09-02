# Sensor health, alerts, and reports

`sensibo collect` now watches every sensor it polls: a location that goes
quiet or whose parent pod drops offline is flagged within a few poll cycles,
its outage is recorded with a start and (when it recovers) an end, and a
daily and weekly SVG chart of everything collected arrives without anyone
opening the dashboard.

> **Unofficial community tool.** Sensibo is a trademark of Sensibo Ltd. This
> project is not affiliated with, endorsed by, or supported by them.
>
> **Local execution, no exceptions.** Health tracking, alerting, and report
> generation all run *inside* the `sensibo collect` process. Every payload
> and every CLI response this page describes carries
> `execution: local (stops when this daemon stops)`. There is no cloud
> fallback: stop `sensibo-collect.service` and every one of these features
> stops with it — see "The local-execution caveat" below.

## Why this exists

Before this landed, staleness was a flag you had to notice by looking:
`room list`, the web dashboard, and the MCP `room_list` tool would each
compute a STALE flag from a location's last-seen time, but nothing recorded
*when* a sensor went quiet, nothing told the operator, and the threshold
(24h) was tuned for a human glance, not a ~90-second-cadence sensor.

Two real outages on the operator's own fleet motivated the design:

- The spare-room Room Sensor (`ms_o7dH4GeY`) went silent on **2026-02-10
  05:52** and stayed silent until it resumed at **2026-09-02 07:49** — about
  seven months, discovered only by directly querying the store for this
  project.
- The bedroom Room Sensor (`ms_kDup7cVx`) went silent on **2026-08-02
  23:00** and resumed at **2026-09-02 07:51** — about thirty days — and
  came back within two minutes of the spare-room sensor. That near-
  simultaneous recovery is itself evidence: a shared parent-pod or BLE-side
  cause is at least as likely as two independent battery deaths, which is
  exactly why health status distinguishes "this sensor is down" from "its
  parent pod is down" (see "The three outage classes" below).

### Reproducing the outage dates from the local store

Both dates are reproducible directly against `~/.sensibo/sensibo.db` with no
code from this project involved — just the gap between consecutive readings
for a location:

```sql
-- Longest silent gap per location, and when it started/ended.
SELECT
    location_id,
    MIN(timestamp)                                   AS first_reading,
    MAX(timestamp)                                   AS last_reading,
    (
        SELECT MAX(gap_end - gap_start) FROM (
            SELECT
                timestamp AS gap_start,
                LEAD(timestamp) OVER (ORDER BY timestamp) AS gap_end
            FROM readings r2
            WHERE r2.location_id = r1.location_id
        )
    )                                                 AS longest_gap_seconds
FROM readings r1
GROUP BY location_id;
```

To see the spare-room outage's exact bounds:

```sql
SELECT MIN(timestamp), MAX(timestamp)
FROM readings
WHERE location_id = 'ms_o7dH4GeY'
  AND timestamp < strftime('%s', '2026-02-11');   -- readings before the gap

SELECT MIN(timestamp)
FROM readings
WHERE location_id = 'ms_o7dH4GeY'
  AND timestamp > strftime('%s', '2026-09-01');   -- first reading after the gap
```

The bedroom sensor's 2026-08-02 → 2026-09-02 gap, and the ~2-minute recovery
offset between the two sensors, is the same query against
`ms_kDup7cVx`, then comparing its post-gap `MIN(timestamp)` to
`ms_o7dH4GeY`'s. Once `sensibo query health` has run against a store with
health data, the same story reads off `outages` directly — no raw SQL
needed; see "Querying health" below.

## The three outage classes

The health evaluator (`sensibo/health/evaluate.py`) distinguishes three
shapes of outage, because they call for different responses and, in the
fan-out case, a different alert volume:

1. **Single sensor down, parent alive.** One Room Sensor (or a Sky/Air pod
   with no children) stops reporting while everything else keeps going.
   This is the genuine "battery died" or "sensor fell off the wall" case —
   status `down`, one notification.
2. **All children of one pod down, pod itself alive or unclear.** Both of
   the operator's Room Sensors going dark within two minutes of each other,
   while nothing says the parent pod itself stopped — this is the ambiguous
   case the near-simultaneous 2026-09-02 recovery illustrates. The evaluator
   still marks each child independently unless the *parent* is itself
   `down`; see the honest caveat in "Open questions" below.
3. **The parent pod down, so every child is unexplained-by-itself.** When a
   pod is `down`, its Room Sensors (identified via `locations.parent_pod_id`
   — see `docs/sensibo-api.md`, "Trap 2: Room Sensor is not a pod") are
   marked `unknown_parent_down` instead of `down`. Exactly one notification
   fires, naming the pod; the children stay silent. This is what stops one
   pod outage from fanning out into N alerts.
4. **All locations down at once — the collector itself failed.** A cloud
   5xx, an exhausted 429 retry, or an expired API key makes the whole fleet
   snapshot fail. Every location is marked `unknown` (never `down`), and
   exactly one `collector_unhealthy` notification fires instead of one per
   sensor. On the operator's live store, the largest gap in the trailing 14
   days (1034 seconds) hit all three locations at the same instant — the
   signature this class is built to recognize instead of misreporting as
   three simultaneous sensor deaths.

Class 4 is reproducible from the store the same way as the single-sensor
case, just filtered across every location for a shared gap window:

```sql
-- Gaps shared across every location at (about) the same instant —
-- evidence of a collector-wide failure, not independent sensor deaths.
SELECT location_id, timestamp AS gap_start,
       LEAD(timestamp) OVER (PARTITION BY location_id ORDER BY timestamp) AS gap_end
FROM readings
ORDER BY gap_start DESC
LIMIT 50;
```

## Health states

Every sensing location has exactly one current state in the `health` table,
computed by `sensibo/health/evaluate.py` (a pure function — no clock, no
store, no network) and persisted by the collector after every poll cycle:

| Status | Meaning |
|---|---|
| `ok` | Reporting inside the threshold, and (for a Room Sensor) its parent pod is not down. |
| `down` | Its own last reading is older than the threshold, or `connectionStatus.isAlive` reports `False`. A location with no reading at all — an id the fleet lists with nothing behind it — is also `down`: that is exactly the battery-dead case on first contact. |
| `unknown` | The collector's own poll cycle failed this round, so nothing can be said about this location specifically. Never `down` in this case — a fleet-wide false alarm is worse than an honest "don't know". |
| `unknown_parent_down` | A Room Sensor whose parent pod is `down`. The child's silence is explained by the parent, so it is not treated as its own sensor-down event and does not notify on its own. |

The evaluator judges collector health first (class 4 above short-circuits
everything to `unknown`), then parents before children (so a child sees its
parent's *this-cycle* settled status), then applies debounce.

## Thresholds and configuration

All of it lives in `sensibo.health.model.HealthConfig`, built via
`HealthConfig.from_env()`:

| Setting | Env var | Default | Notes |
|---|---|---|---|
| Down threshold | `SENSIBO_HEALTH_DOWN_AFTER` | `900` (seconds) | About ten missed cycles at the measured 91s cadence — see `docs/sensibo-api.md`, "Poll cadence: measured, not assumed". This is also the *one* source of truth for the STALE flag on `room list`, the web dashboard, and the MCP `room_list` tool — an operator's override changes all of them together. |
| Recovery hold | — (not env-configurable) | `2` consecutive good evaluations | A location must report inside the threshold for two straight cycles before it clears `down` back to `ok`. A sensor that flaps every cycle never satisfies this and never "recovers" mid-flap. |
| Cooldown | `SENSIBO_HEALTH_COOLDOWN` | `3600` (seconds) | Minimum spacing between repeat `down` notifications for one location. |
| Daily cap | `SENSIBO_HEALTH_DAILY_CAP` | `20` | Hard ceiling on notifications per location per UTC day; further candidates are counted as suppressed rather than sent. |

An unparseable or negative value for any of these raises rather than
silently falling back — a threshold the operator believes they set but
which was ignored is worse than a loud failure at startup.

### Debounce and the daily cap, concretely

A `down` candidate fires the first cycle a location crosses the threshold
(the threshold itself is already ~10 missed cycles of slack). A `recovered`
candidate only fires once the recovery hold is satisfied, **and** only if
the outage it is closing was itself announced — an outage that was
suppressed by cooldown or the daily cap gets no closing message either,
which is what keeps a flapping sensor to at most one down and one recovery
per cooldown window rather than a stream of both. The bedroom sensor's
history motivated this: nine gaps over six hours in July 2026 alone would,
un-debounced, have produced up to eighteen messages from one sensor in a
month.

### First-run seeding

The very first evaluation against an empty health map treats every location
as unseen: any location already past the down threshold at that instant is
seeded straight to `down` and gets **one** down notification, so an outage
that predates the upgrade (the seven-month spare-room gap, for instance) is
announced immediately rather than silently adopted as the new baseline.
Locations already inside the threshold are seeded to `ok` with no
notification.

## Collector health, distinct from sensor health

A cloud outage, an expired key, or an exhausted 429 retry no longer crashes
the daemon. Before this change, `sensibo collect --daemon`'s loop let an
`ApiError` propagate out and exit the process with code 2 — `Restart=always`
was the only thing that brought it back, and a permanently bad API key
produced an endless systemd restart loop with no notification at all.

Now: a failed poll cycle still records the failure (`meta.last_cycle_outcome
= "failed"`), runs the health evaluator with a failed `CollectorOutcome` (so
every location goes `unknown`, never `down`), and only *then* re-raises the
`ApiError` for the caller. `sensibo collect --once` still exits 2 on
failure — a one-shot run should tell you plainly that it failed. The
**daemon** catches it, logs to stderr, and keeps its normal interval instead
of dying. The health engine treats the transition edge-triggered: exactly
one `collector_unhealthy` notification fires on the first failed cycle after
a healthy one, and exactly one `collector_recovered` fires on the first
success after a run of failures — not one notification per failed cycle.

**Consequence for `Restart=always`:** a permanently bad API key used to
produce a fast crash-restart loop, visible in `systemctl --user status
sensibo-collect.service` and the journal. It now produces `collector_
unhealthy` notifications on the configured cooldown instead, and the
service itself stays `active` — indistinguishable from healthy by process
status alone. `sensibo doctor`'s `collector_heartbeat` check and `sensibo
query health --json`'s `collector` block are how you tell the two apart;
see "The heartbeat" below.

## The heartbeat

After every cycle — success or failure — the collector writes two facts to
the store's `meta` table: `last_cycle_at` (epoch seconds) and
`last_cycle_outcome` (`"ok"` or `"failed"`). Three places read it:

- `sensibo query health --json` includes a `collector: {last_cycle_at,
  last_cycle_outcome}` block on every response.
- `sensibo doctor` gains a `collector_heartbeat` check: healthy iff a
  heartbeat is present and no older than 3x the configured poll interval.
  A collector that stopped running entirely (not even failing — just gone,
  e.g. the host is off) produces no `collector_unhealthy` notification, since
  nothing is running to detect its own absence; the heartbeat check is the
  only thing that surfaces that case, and only when someone runs `doctor`.
- The web dashboard's index page header shows `last_cycle_at` /
  `last_cycle_outcome` directly, so "no alerts firing" can be told apart
  from "the collector stopped running" at a glance.

**Notification backlog (`health_owed`).** If a notification cannot be
delivered on the cycle its transition happened (a transport failure, or the
notify config being briefly unreachable), the collector keeps it in a
`meta`-persisted backlog (`health_owed`) and retries delivery on the next
cycle rather than dropping it — a transient webhook failure does not silently
lose an alert.

## Notification configuration

Resolution mirrors the API key (`CLAUDE.md`, "Secrets"): the environment
first, then `~/.sensibo/.env` (chmod 600).

| Variable | What |
|---|---|
| `SENSIBO_NOTIFY_WEBHOOK` | A URL. Delivery is a plain `urllib` `POST` of a compact JSON payload, `Content-Type: application/json`, a 10-second timeout. Non-2xx and network errors are returned as a failed outcome, never raised — a flaky webhook must not take down the collector cycle that detected the outage. |
| `SENSIBO_NOTIFY_SCRIPT` | A path. Delivery is `subprocess.run` with a fixed one-element argv list (the script path — no shell, no argument splitting), the JSON payload on **stdin**, a 10-second timeout, and a child environment with `SENSIBO_API_KEY` and `SENSIBO_NOTIFY_WEBHOOK` **removed** — an operator's own script cannot echo either secret back into a log or its own output. |

Either, both, or neither may be set. With neither configured, `send()`
returns a `"not configured"` outcome and never raises; `sensibo notify test
--apply` with nothing configured is a user error (exit 1) instead, since
sending would otherwise be a silent no-op.

### Payload shape

```json
{
  "kind": "down",
  "location": "ms_o7dH4GeY",
  "status": "down",
  "since": "2026-09-02T05:52:00Z",
  "last_ok": "2026-09-02T05:51:09Z",
  "message": "room_sensor ms_o7dH4GeY is down as of 2026-09-02T05:52:00Z: last heard 2026-09-02T05:51:09Z, past the 900s threshold",
  "execution": "local (stops when this daemon stops)"
}
```

`kind` is one of `down`, `recovered`, `collector_unhealthy`,
`collector_recovered`, `report`, or `test` (`sensibo notify test`'s own
payload). All timestamps render ISO-8601 UTC (`sensibo.health.iso8601`),
never host-local — a notification is read later, elsewhere, possibly by an
agent, and an ambiguous stamp there is a bug.

### Redaction, everywhere

The webhook URL is a secret — a Discord webhook URL grants post rights —
and is treated exactly like the Sensibo API key
(`sensibo/api/_scrub.py`): `sensibo.notify.transport.redact()` replaces any
occurrence of the configured URL with `https://…(redacted)` in:

- every log line a failed delivery might produce,
- every `--json` payload (`sensibo notify test`, `sensibo report`),
- the dry-run preview text (`render_dry_run`) — the URL never reaches a
  terminal or an agent transcript even when nothing was actually sent.

### `sensibo notify test`

```bash
sensibo notify test              # dry-run: prints the exact redacted payload, sends nothing
sensibo notify test --json       # same, structured
sensibo notify test --apply      # sends exactly once per configured transport
```

Dry-run by default, like every write verb in this repo. `--apply` calls the
transport(s) **exactly once** and prints each one's outcome (`ok` /
`FAILED`, with a redacted detail). See `sensibo explain notify`.

## Reports

`sensibo report daily|weekly` renders a self-contained, stdlib-only SVG
document straight from the local store — one panel per (location, numeric
field) pair, no network access, no external assets, no JavaScript
(`sensibo.report.chart.render_report`, built on the same even-downsampling
approach as the web dashboard's `sensibo/web/_svg.py` sparklines). `daily`
covers the trailing 24 hours; `weekly` the trailing 7 days.

### Why SVG, and why no multipart upload

**No new runtime dependency** (`pyproject.toml` keeps `dependencies = []`):
a rasteriser (PNG/JPEG) needs a real image library or an external binary,
neither of which is stdlib. SVG is XML text a `datetime`/`html`-only
renderer can produce directly, and it is a decision recorded up front —
Discord will not preview an SVG inline, and that is accepted rather than
worked around with a dependency.

The delivered notification for a report (`kind: "report"`) never uploads the
file itself — it carries only the **path** the report was written to (plus a
dashboard link when `SENSIBO_DASHBOARD_URL` is configured), exactly like
every other notification this project sends. An operator's own script hook
may choose to read the path and upload the file itself; this project does
not do multipart uploads.

### Where reports live

```text
~/.sensibo/reports/daily-YYYY-MM-DD.svg      # UTC calendar day
~/.sensibo/reports/weekly-YYYY-Www.svg       # ISO week number
```

Override the directory with `SENSIBO_REPORTS_DIR`; it is created with mode
`0700` on first write. `sensibo web` serves the directory read-only under
`/reports/<name>` (open reads, same trust model as every other `GET` route —
see `docs/web.md`), listed newest-first on the dashboard index.

### Scheduling

The collect daemon calls the report scheduler after **every** cycle,
success or failure — a scheduling misconfiguration or a delivery hiccup here
must never take down collection. A report is due when the most recent
scheduled instant at or before `now` is later than that kind's last-sent
instant, persisted in `meta` as `last_daily_report_at` /
`last_weekly_report_at`. That "most recent instant, not missed count" rule
is what makes restarts safe: a daemon down across several missed 07:00's
gets **at most one** catch-up report per kind when it comes back, never a
backlog of one per missed day; a restart exactly at 06:59 and again at 07:01
still yields exactly one daily report.

| Setting | Env var | Default |
|---|---|---|
| Daily time | `SENSIBO_REPORT_DAILY_AT` | `07:00` (host-local) |
| Weekly time | `SENSIBO_REPORT_WEEKLY_AT` | `07:00` (host-local) |
| Weekly day | `SENSIBO_REPORT_WEEKLY_DAY` | `0` (Monday; `0`-`6`, `0`=Monday) |

### `sensibo report daily|weekly`

```bash
sensibo report daily                        # dry-run: prints where it would write/notify
sensibo report daily --out /tmp/today.svg   # also write the SVG to an explicit path (no --apply needed)
sensibo report daily --apply                # writes into the reports directory, records it sent, delivers
sensibo report weekly --apply --json
```

`--out PATH` writes without requiring `--apply` — an explicit output path is
the operator asking for a file, the same convention every other verb with an
explicit path argument follows. `--apply` is what marks the kind's
scheduling clock satisfied and delivers the notification, exactly once. No
new systemd unit is required: reports run inside `sensibo-collect.service`
alongside collection and health, on the same in-daemon clock — see
`docs/deployment.md`.

## Querying health

```bash
sensibo query health                                  # every location
sensibo query health "Living Room"                     # one location, by id/alias/room name
sensibo query health ms_o7dH4GeY --since 2026-08-01T00:00:00Z --json
```

Offline only — reads exclusively from the local store, same promise as every
other `query` verb (a socket-blocking guard test enforces it). The response
carries each location's current `status`/`since`/`last_ok`, the raw
transition log (optionally narrowed by `--since`), a computed `outages` list
(closed `down`→`ok` pairs with `start`, `end`, `duration_seconds`), and the
`collector` heartbeat block. An empty `health` table (no cycle has run yet)
is the same "empty store" remediation every other `query` verb gives.

## Rules: the `stale` leaf

The rules grammar gained exactly one new leaf type — nothing else in
`sensibo/rules/` changed:

```json
{"type": "stale", "location": "Bedroom", "after_seconds": 900}
```

`after_seconds` is optional; when omitted, "stale" means the location's
persisted health status is anything other than `ok`. When given, it also
counts as stale if `last_ok` is older than that many seconds — even if the
health table hasn't caught up to `down` yet. A location with **no** health
row at all counts as stale too: silence this project cannot explain is not
evidence of health.

`sensibo rule dry-run` shows a `stale` leaf's evaluation alongside every
other leaf, naming the location's current status and `last_ok`. The
compressor safety gates — minimum off-time/hysteresis, the one-write-per-pod
rate limit, and the arm-requires-fresh-dry-run contract — are completely
unchanged; this is a read of persisted health, nothing more.

`examples/stale-room.rule.json` ships a worked example: it turns a bedroom
AC off as soon as the bedroom's Room Sensor is stale, rather than trusting
whatever motion or temperature reading happened to be its last one before it
went silent.

## Schema v2 and the upgrade note

`sensibo/store/_schema.py`'s `SCHEMA_VERSION` is now `2`. Three new tables:

| Table | Columns |
|---|---|
| `health` | `location_id` (PK), `status`, `since`, `last_ok`, `parent_pod_id` — one row per location, current state only. |
| `transitions` | `id` (PK, autoincrement), `location_id`, `from_status`, `to_status`, `at`, `notified_at` — append-only log of every status change; `notified_at` is `NULL` until an alert for that transition actually went out. |
| `notifications` | `id` (PK, autoincrement), `kind`, `location_id`, `sent_at`, `transport`, `outcome` — append-only log of every delivery attempt, successful or not. |

Plus a one-off, idempotent migration: existing `batteryVoltage` readings
written by a v1 binary carry a `NULL` unit (v1's unit map didn't know the
field), and are backfilled to `mV` inside a transaction guarded by a `meta`
flag (`units_backfill_v2`) — running it twice changes zero rows the second
time.

**Fail-closed version check.** `init_schema` now reads `PRAGMA user_version`
and compares it against `SCHEMA_VERSION` before doing anything else:

- **v2 binary opening a v1 (or fresher-but-lower) file** — creates the new
  tables (`CREATE TABLE IF NOT EXISTS`) and stamps the version up to 2. An
  in-place upgrade, no data movement.
- **v2 binary opening an already-v2 file** — nothing to create; the version
  stamp is skipped since it already reads `SCHEMA_VERSION`.
- **v2 binary opening a file stamped by a *newer* build (version > 2)** —
  raises `StoreVersionError` instead of opening it. The error carries a
  remediation: upgrade to a build that understands that schema version, or
  point `SENSIBO_DB` at a different file. No traceback path — every process
  that calls `init_schema` (collect, query, web, mcp, rule, notify, report)
  gets the same structured failure.

**What a v1 binary does against a v2 file, concretely.** A v1 binary's
`init_schema` predates this fail-closed check — it has no code path that
even looks at whether the file is newer than it expects, and its own
`CREATE TABLE IF NOT EXISTS` calls only know about the v1 tables (it has
never heard of `health`, `transitions`, or `notifications`, so it neither
touches nor breaks them). It **can** still open the file and keep writing
`locations` and `readings` rows into it without error — those tables are
byte-for-byte unchanged between v1 and v2. The hazard is narrower and
specifically about the version stamp: an *old*, pre-this-change v1 binary
unconditionally executed `PRAGMA user_version = 1` on every connect. If such
a binary is still running against an already-migrated v2 file — the exact
situation during a rolling upgrade, before the daemon has been restarted —
it would silently stamp the file's version back down to 1. A later v2 open
would then see `user_version = 1` and believe it needs to "upgrade" a file
that was never really v1, which is harmless today (the migration is
idempotent) but is exactly the kind of two-binaries-disagree state a
version stamp exists to prevent. This build's `init_schema` only ever
*raises* the stamp, never lowers it, which is what makes the two binaries
safe to overlap during the restart window.

**Restart order.** Upgrade the `sensibo-cli` package/venv first, then:

```bash
systemctl --user restart sensibo-collect.service
```

so the daemon that is actually writing to the store is the v2 binary before
anything else (a manual `sensibo query health`, `sensibo report daily`,
etc.) relies on the new tables being populated. `sensibo web` and `sensibo
mcp serve`, if run as separate long-lived processes, should be restarted the
same way — see `docs/deployment.md`.

## The local-execution caveat

Every notification, every report delivery, and every health evaluation in
this document runs **inside `sensibo collect`**, on this operator's own
machine — exactly like the local rules engine
(`sensibo/rules/model.py`'s `EXECUTION_LOCAL`, `docs/roadmap.md`, "every
rule must state, in its own output, where it runs"). There is **no cloud
fallback** anywhere in this system:

- Stop `sensibo-collect.service` and health evaluation, alerting, and report
  generation all stop with it. `Restart=always` (`docs/deployment.md`)
  brings the *process* back after a crash, but a **powered-off host**
  produces no down-alert for anyone, ever — the collector daemon is itself
  the thing that would notice a sensor going quiet, and it cannot notice its
  own absence.
- The only visibility into "is anything actually watching" is the heartbeat
  (`sensibo doctor`'s `collector_heartbeat` check, or `sensibo query
  health --json`'s `collector` block) — and only when someone thinks to run
  it. This is a named, accepted gap, not a hidden one: there is currently no
  opt-in "still alive" heartbeat notification shipped, only the passive
  heartbeat fact in the store.
- Every notification and report payload carries `execution: local (stops
  when this daemon stops)` precisely so a receiver — human or agent — never
  mistakes this for Sensibo's own cloud-resident automation
  (`smartmode`/`schedule`/`timer`), which keeps running while this machine
  sleeps.

## Open questions

Carried over from the spec's parked risks, honestly, not smoothed over:

- **The real battery-dead voltage threshold is unknown.** Two observed
  last-before-silence values (1638 mV, 1728 mV, one sensor each) are not
  enough to fix a low-voltage warning level, and the field is quantized
  (flat at 3000 mV for at least 14 days, then a cliff) — a threshold tuned
  against a smooth discharge curve might never fire before the sensor dies.
  The down alert (silence past the threshold) is the trustworthy signal
  today; a voltage-drop warning stays a `[follow_up]`, not implemented.
- **Whether Sensibo itself flips `connectionStatus.isAlive` to `False` for
  a battery-dead Room Sensor, or only freezes its measurements**, is
  unconfirmed — both of the operator's sensors read as online after they
  recovered, and the store never recorded the value during either outage.
- **Two writers on one sqlite file** — the collect daemon, `sensibo notify
  test --apply`, and `sensibo report --apply` run concurrently against the
  same store. WAL mode and sqlite's default 5-second busy timeout are
  assumed sufficient; this is untested under real concurrent load.

## See also

- [`docs/sensibo-api.md`](sensibo-api.md) — the `batteryVoltage` field
  correction and the measured poll-cadence probe this page's default
  threshold is derived from.
- [`docs/deployment.md`](deployment.md) — the systemd units health/alerting
  and reports now run inside, and the restart order after an upgrade.
- [`docs/web.md`](web.md) — the dashboard's health status display and
  `/reports/` route.
- [`docs/mcp.md`](mcp.md) — the `sensibo_health` MCP tool.
- `sensibo explain query health`, `sensibo explain notify`, `sensibo explain
  report` — the same detail from the CLI itself.
