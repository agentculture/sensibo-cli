# Deployment: the always-on host

`sensibo service` installs the collector and the dashboard as systemd **user**
units, so they survive your terminal closing, your logout, and a reboot.

> **Unofficial community tool.** Sensibo is a trademark of Sensibo Ltd. This
> project is not affiliated with, endorsed by, or supported by them.

This page closes the open question the product spec parked under
[`docs/specs/2026-07-14-sensibo-cli-ships-the-full-product-one-cli-and-age.md`](specs/2026-07-14-sensibo-cli-ships-the-full-product-one-cli-and-age.md)
("Open / follow-up"):

> Always-on host for the collector and rules daemon (which machine, systemd
> unit, restart policy) — the deployment story.

## Why this is not a convenience feature

Directly above that open question, the same spec records a risk:

> If the accessible cloud window is really about 7 days, any collection gap
> (host asleep for a week) is permanently lost data — the collector needs an
> always-on home.

That is the whole argument. Sensibo's cloud serves roughly the last **7 days**
of history ([`docs/sensibo-api.md`](sensibo-api.md)), so a gap in local
collection is not a delayed sync that catches up later — it is **data you can
never get back**. The retention pillar is exactly as good as the collector's
uptime, and a foreground `collect --daemon` in a terminal has the uptime of
that terminal.

There is a second, sharper reason, though it is narrower than it used to be.
`sensibo collect --daemon` used to be **not internally resilient**: an
`ApiError` — a cloud 5xx, an exhausted 429 retry, or simply the network not
being up yet at boot — propagated out of its loop and exited the process
with code 2. Since the sensor-health work (`sensibo/cli/_commands/collect.py`,
`docs/health.md`), the daemon **survives** an `ApiError`: it records the
failed cycle, runs the health evaluator (every location goes `unknown`, one
`collector_unhealthy` notification fires), logs to stderr, and keeps its
normal interval. `sensibo collect --once` still exits 2 on failure, and
`Restart=always` still matters — a crash the daemon's own loop can't catch
(an unhandled exception, an OOM kill) still needs it, and a **permanently bad
API key now produces a `collector_unhealthy` notification on the configured
cooldown instead of a fast crash-restart loop**, which reads as `active` in
`systemctl --user status` even though nothing is actually being collected.
`sensibo doctor`'s `collector_heartbeat` check (`docs/health.md`, "The
heartbeat") is how you catch that case.

## Quickstart

```bash
sensibo service install            # dry-run: prints every file and command
sensibo service install --show-units   # ... and the full unit bodies
sensibo service install --apply    # commit
sensibo service status             # is it actually collecting?
```

`install` is **dry-run by default**, like every write verb in this CLI. The
guarantee is structural, not a flag check: the plan builders in
`sensibo/service/manager.py` are pure functions that describe the writes and
perform none of them, so the path that mutates your system does not execute
without `--apply`.

## What gets installed

Three files in `~/.config/systemd/user/` — no root, no `sudo`, nothing in
`/etc`:

| Unit | Runs | Restart |
|---|---|---|
| `sensibo-collect.service` | `sensibo collect --daemon --interval 60` | `always`, after 30s |
| `sensibo-web.service` | `sensibo web --bind 0.0.0.0:8323` | `always`, after 5s |
| `sensibo.target` | groups both; `WantedBy=default.target` | — |

Plus one command that is easy to overlook and does most of the work:

```bash
loginctl enable-linger $USER
```

**Lingering is what makes this always-on rather than always-on-while-logged-in.**
A systemd *user* manager normally starts when you log in and dies when you log
out — without lingering, these units would stop the moment you closed your SSH
session, and would not come back at boot. With it, systemd starts your user
manager at boot, no login required. `install` enables it for you (and says so
in the plan); `status` reports it; `uninstall` deliberately leaves it alone,
because you may well have enabled it for something else.

## Sensor health, alerts, and reports run inside the same unit

**No new systemd unit is needed for health tracking, alerting, or reports.**
All three run inside `sensibo-collect.service`: health evaluation happens
after every poll cycle, and the report scheduler runs in the same daemon
loop against its own in-daemon clock (`SENSIBO_REPORT_DAILY_AT` /
`SENSIBO_REPORT_WEEKLY_AT`, default `07:00` host-local — see
[`docs/health.md`](health.md)). Reports are written to
`~/.sensibo/reports/` (override with `SENSIBO_REPORTS_DIR`) and served
read-only by `sensibo-web.service` at `/reports/`.

**Restart order after an upgrade.** The store's schema went from version 1
to version 2 with this change (new `health`/`transitions`/`notifications`
tables, a fail-closed version guard — see `docs/health.md`, "Schema v2 and
the upgrade note"). After upgrading the installed `sensibo-cli` package or
venv, restart the collector so the running daemon is the v2 binary:

```bash
systemctl --user restart sensibo-collect.service
```

Do the same for `sensibo-web.service` if it needs the new health columns on
the dashboard. Until restarted, an old v1 daemon keeps writing readings
into the (already-migrated) v2 file without error — the v1 tables are
unchanged — but produces no health/alert/report data. See `docs/health.md`
for exactly what a not-yet-restarted v1 binary does and does not do against
a v2 file.

## What is deliberately NOT installed

**`rule run --daemon`.** It evaluates armed rules and **drives a real
compressor** unattended. Turning that on is an explicit operator decision, not
a side effect of asking for collection to stay up. `service install` prints a
line saying so, and no unit it writes ever mentions `rule` (there is a test).

If you want the rules daemon supervised too, that is tracked as a follow-up —
it needs its own thinking about what happens when a rules daemon restarts
mid-hysteresis-window, which is not the same problem as restarting a poller.
Until then, run it in the foreground where you can watch it.

## The API key

**No unit file names your API key**, and there is a test that keeps it that
way. A unit file in `~/.config/systemd/user/` is world-readable; a key does not
belong in one.

Instead, the key resolves *inside the client* at runtime — `SENSIBO_API_KEY`
from the environment first, then `~/.sensibo/.env` (mode 600). systemd never
parses the dotenv, never holds the key, and never logs it. This also sidesteps
a real trap: systemd's `EnvironmentFile=` format is not shell syntax, so a
`.env` written with `export K=v` or quoted values would be parsed wrong (or
silently produce a mangled key) if it were wired in that way.

Make sure `~/.sensibo/.env` exists and is mode 600 before you `--apply`, or the
collector unit will restart-loop on a missing key. `sensibo doctor` and
`sensibo service status` will both show you the damage.

## Checking it works

```bash
sensibo service status
```

```text
sensibo service status
execution: local (systemd-supervised; survives logout and reboot)

unit dir: /home/you/.config/systemd/user
linger:   enabled for you (units start at boot, no login needed)

  sensibo-collect.service  enabled   active
  sensibo-web.service      enabled   active
  sensibo.target           enabled   active

store: /home/you/.sensibo/sensibo.db
  3 location(s), newest reading 41s ago
```

The last line is the one that matters. `active` only means the process is
running; **`newest reading` is the only proof that collection is actually
landing data.** An `active` collector whose newest reading is 40 minutes old is
a broken collector — it is restart-looping, or failing every cycle against the
cloud. Read the journal when that happens:

```bash
journalctl --user -u sensibo-collect -f      # follow the collector
journalctl --user -u sensibo-web -n 50       # last 50 dashboard lines
```

## Flags

```bash
sensibo service install --apply --interval 120        # slower poll (floor: 60s)
sensibo service install --apply --bind 127.0.0.1:8323 # dashboard on loopback only
sensibo service install --apply --no-web              # collector only
sensibo service install --apply --no-collect          # dashboard only
sensibo service install --apply --db /srv/sensibo.db  # non-default store
sensibo service install --apply --exec-path /path/to/sensibo
sensibo service uninstall --apply                     # disable, stop, delete
```

## The `--exec-path` trap

systemd needs an **absolute path** in `ExecStart=` and inherits none of your
shell's `PATH` or virtualenv activation. `service install` resolves the
`sensibo` console script for you — from `PATH` first, then as a sibling of the
running interpreter (so `uv run sensibo service install` finds the repo venv's
copy).

That resolution is worth reading in the printed plan before you `--apply`. If
it points into a **repo checkout's `.venv`**, the units break the day you
delete or rebuild that venv. For a durable install, put the CLI somewhere
stable and point at it:

```bash
uv tool install sensibo-cli
sensibo service install --apply --exec-path ~/.local/bin/sensibo
```

## Reinstalling after an upgrade

`install --apply` is idempotent: it rewrites the units, re-runs
`daemon-reload`, and re-enables. Safe to run any time. Do run it after changing
`--interval` or `--bind` — those values are **baked into the unit files**, so
editing your shell habits changes nothing until the units are rewritten.

## Not on Linux?

systemd user units need Linux. On macOS or Windows, `install --apply` fails
with an environment error (exit 2) rather than pretending, and points you at
that platform's own supervisor (`launchd`, Task Scheduler). The honest answer
for those platforms today is: run the collector on a Linux box that stays
awake, and point your laptop's `sensibo query` at it — or open an issue if you
want first-class `launchd` support.

## See also

- [`docs/web.md`](web.md) — the dashboard the `sensibo-web.service` unit serves,
  and its reads-open / writes-token-gated trust model.
- [`docs/architecture.md`](architecture.md) — the CLI contracts (`CliError`,
  the stream split) that `service` honors like every other verb.
- `sensibo explain service` — the same summary from the CLI itself.
