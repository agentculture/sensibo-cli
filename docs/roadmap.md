# Roadmap

Build order, and the honest split between automation that runs in Sensibo's cloud
and automation that needs our daemon alive.

## Build order

Do not build the three pillars at once. Each step below makes the next one easier
to reason about, and step 1 is what turns the per-model sensor table in
[`sensibo-api.md`](sensibo-api.md) from research into fact.

1. **`sensibo devices`** — authenticate, list the pods, print what each one
   *actually* reports. This alone answers the schema question with real data, and
   it's how we find out whether the documented per-model sensor sets match the
   operator's real fleet.
2. **`sensibo read <device>`** — one snapshot of every current reading, `--json`.
3. **`sensibo collect`** — the local store: poll on a cadence, persist, query
   offline. The point of the product.
4. **`sensibo set <device> --mode cool --target 24 [--apply]`** — the control
   verb. Dry-run by default.
5. **Rules** — last, once there is real collected data to write conditions
   against.

Ship 1 and 2 first. Everything else gets easier.

## The automation question, answered honestly

Sensibo **already ships** server-side automation: Climate React (threshold → AC
action) and schedules. So "set up automated conditions" splits two ways, and this
CLI should be clear-eyed about which it is doing at any moment.

### Track 1 — manage Sensibo's own automation

Make `sensibo` an agent-first, scriptable, diffable front-end for Climate React
(`/pods/{id}/smartmode`) and schedules (`/pods/{id}/schedules/`).

- **Runs in Sensibo's cloud.** Keeps working when the laptop is closed, the
  daemon is dead, and the house's computer is off.
- Bounded by what Sensibo's engine can express — single-sensor thresholds on one
  pod.
- Cheap, robust, and it stops us reimplementing something that already works.

### Track 2 — our own local rules engine

The differentiator. Conditions Sensibo cannot express:

- multi-sensor and cross-room logic ("hallway motion **and** bedroom above 26 °C")
- air quality driving the AC (Elements' PM2.5, TVOC)
- time-of-day combined with occupancy
- runtime and energy budgets
- hysteresis to stop short-cycling the compressor

- **Requires our daemon to be alive.** If the machine sleeps, these rules stop.

### The rule that matters

**Every rule must state, in its own output, where it runs.** An automation that
silently stops when a laptop sleeps is worse than no automation — the operator
believes their house is being managed and it isn't. `sensibo rules list` must
distinguish cloud-resident rules from daemon-resident ones, and the daemon must
be able to tell you when it last ran.

Do both tracks. Track 1 is the cheap win; Track 2 is the product.

## Safety: this drives compressors in someone's home

Not optional, and not a "later" concern — these constrain the design of tracks 4
and 5 above:

- **Minimum off-time / hysteresis.** A rule must not be able to short-cycle a
  compressor. Short-cycling damages hardware. This belongs in the engine, not in
  each rule's config, so it cannot be forgotten.
- **Rate-limit outbound calls.** Both to respect Sensibo's (real, unpublished)
  rate limit and to avoid thrashing a unit.
- **Every write verb is dry-run by default; `--apply` commits.** Mandatory.
- **"What would this rule do right now?" must be inspectable before it is armed.**
  A rule you cannot dry-run is a rule you cannot trust.

## Answered: the always-on host

The spec parked "always-on host for the collector and rules daemon (which
machine, systemd unit, restart policy)" as an open follow-up. It is answered in
[`deployment.md`](deployment.md): `sensibo service install --apply` writes
systemd **user** units for `collect --daemon` and `web` with `Restart=always`,
and enables `loginctl` lingering so they start at boot without a login.

The rules daemon is **deliberately excluded** from that install — it drives a
compressor unattended, and a restarting rules daemon raises a question a
restarting poller does not (what happens to a hysteresis window mid-restart?).
That stays a foreground, explicit decision until it is thought through.

## Open questions

Tracked in [`sensibo-api.md`](sensibo-api.md#open-questions-to-settle-against-real-hardware).
The ones that block design decisions:

- **Real `historicalMeasurements` retention** — believed ~7 days, unconfirmed.
  Determines how urgent the collector is, and what the README may claim.
- **What the operator's fleet actually reports** — determines the store schema.
- **The real rate limit** — determines the poll cadence.
