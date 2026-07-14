# Real-fleet acceptance walkthrough

Run 2026-07-14 against the operator's real fleet (one `airq` main unit
"Central", two Room Sensors), with `SENSIBO_API_KEY` resolved from
`~/.sensibo/.env`. Every step below executed against the live Sensibo cloud —
none of it mocked. This is the acceptance record for the full-product spec
([`docs/specs/`](specs/)) and plan task t13.

## Control

```console
$ sensibo devices --json          # one fleet call: pod + both Room Sensors,
                                  # per-model field sets (incl. batteryVoltage)
$ sensibo read 8DdxNuyc           # temperature, humidity, feelsLike, motion,
                                  # roomIsOccupied, tvoc, co2, iaq, rssi
$ sensibo set 8DdxNuyc --target 24            # dry-run: prints the diff, no write
$ sensibo set 8DdxNuyc --target 24 --apply    # PATCH single property, read back
$ sensibo set 8DdxNuyc --target 25 --apply    # restored; unit stayed off throughout
```

Verified: set-then-read-back round-trips (25 → 24 → 25); a write without
`--apply` changes nothing on the unit.

## Collection

```console
$ sensibo collect --once --json
{"locations_seen": 3, "pods": 1, "room_sensors": 2, "readings_written": 9,
 "backfill": {"ran": true, "window_days": 1, "readings_written": 9894}, ...}
$ sensibo query latest 8DdxNuyc --field temperature --json   # offline, from SQLite
$ sensibo query locations
```

Verified: the first cycle probed `historicalMeasurements` descending
(730 → … → 1), hit the account's real gate (`days=1`, everything above 403s),
backfilled 9,894 readings, and recorded the window. Later cycles skip the
probe. Queries answer from `~/.sensibo/sensibo.db` with no network.

## Room naming and staleness

```console
$ sensibo room name 8DdxNuyc central --apply
$ sensibo room name ms_kDup7cVx bedroom --apply
$ sensibo room name ms_o7dH4GeY spare-room --apply
$ sensibo room list
... ms_o7dH4GeY ... alias=spare-room last_seen=2026-02-10T05:52:21 STALE
```

Verified: aliases persist across polls; the Room Sensor that stopped
reporting in February is flagged STALE. (This walkthrough caught and fixed a
real bug: `last_seen` had been stamped with the poll instant, which made the
dead sensor look alive forever.)

## Automation

```console
$ sensibo rule add --file examples/cross-room-motion-temp.rule.json  # adapted to
                                                                     # this fleet
$ sensibo rule dry-run cool-when-bedroom-busy-and-hot
  [unmet] occupancy in bedroom: motion=vacant, want occupied
  [MET ] threshold temperature in central: 30.8 > 26
$ sensibo rule arm cool-when-bedroom-busy-and-hot     # only allowed after dry-run
$ sensibo rule disarm cool-when-bedroom-busy-and-hot
$ sensibo smartmode show 8DdxNuyc     # execution: cloud (survives daemon sleeping)
$ sensibo schedule list 8DdxNuyc      # found the operator's real (disabled) schedule
$ sensibo timer show 8DdxNuyc         # no timer set — rendered as a state, not an error
```

Verified: cross-room conditions resolve locations by operator alias against
real store data; a rule cannot arm without a dry-run of its current
definition; local vs cloud execution is declared on every output. (The
walkthrough also resolved the v1-vs-v2 question for `timer/` and `schedules/`
— see [`docs/sensibo-api.md`](sensibo-api.md).)

## Integration surfaces

```console
$ python3 -c "import sensibo; print(sensibo.Client().fleet_snapshot())"
# lists the fleet; argparse never imported
$ python3 -c "from sensibo.mcp_server import _tools; print(_tools.list_devices())"
# the MCP tools against the real fleet; stdio transport proven in the suite
$ sensibo web --bind 127.0.0.1:8399 &
$ curl http://127.0.0.1:8399/                       # dashboard from the local store
$ curl http://127.0.0.1:8399/api/history?...        # serves the backfilled series
$ curl -X POST .../api/set -d '{"pod_id": ...}'     # 401 without the token
$ curl -X POST .../api/set -H "X-Sensibo-Token: $(cat ~/.sensibo/web-token)" ...
{"applied": false, "changes": {"targetTemperature": {"from": 25, "to": 24}}}
# token + no confirm = dry-run diff, zero writes — same contract as the CLI
```

## What the walkthrough changed

Real-fleet acceptance is why this step exists; it caught three defects the
mocked suite could not:

1. `last_seen` stamped from the poll clock — dead sensors never went stale.
2. `timer/` and `schedules/` are v1 endpoints — the v2 routes are server-level
   404s; the OpenAPI spec is wrong.
3. Gzipped HTTP *error* bodies were embedded undecoded in error messages.

All three are fixed and covered by tests.
