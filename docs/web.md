# Web dashboard

`sensibo web` serves a LAN dashboard over stdlib `http.server`
(`ThreadingHTTPServer`) — zero runtime dependencies, no external assets, no
JavaScript framework. It is task t12 of the full-product build; see
[`docs/plans/2026-07-14-sensibo-cli-ships-the-full-product-one-cli-and-age.md`](plans/2026-07-14-sensibo-cli-ships-the-full-product-one-cli-and-age.md)
(task t12) for the acceptance criteria this page documents.

> **Unofficial community tool.** Sensibo is a trademark of Sensibo Ltd. This
> project is not affiliated with, endorsed by, or supported by them.

## Quickstart

```bash
export SENSIBO_API_KEY=...          # only needed for the control form's writes
uv run sensibo collect              # populate the local store at least once
uv run sensibo web                  # bind 0.0.0.0:8323 (LAN-reachable)
```

The first run prints, to stderr, where the write-auth token was written —
never the token's value:

```text
sensibo web: token file: /home/you/.sensibo/web-token
sensibo web: reads are OPEN to anyone who can reach this host; writes require the token in /home/you/.sensibo/web-token
sensibo web: serving on http://0.0.0.0:8323/ (Ctrl-C to stop)
```

Open `http://<this-machine>:8323/` from any browser on the LAN. To control a
pod, copy the token out of the printed file path and paste it into the
control form's **Token** field.

Flags:

```bash
sensibo web --bind 127.0.0.1:8323                       # loopback only
sensibo web --db /path/to/sensibo.db                     # non-default store
sensibo web --token-file /path/to/token                  # non-default token location
sensibo web --json                                        # startup summary as JSON on stdout
```

## The reads-open / writes-token-gated model

This is a **recorded operator decision**, not an oversight — from the
project's `devague` spec (`docs/specs/2026-07-14-sensibo-cli-ships-the-full-product-one-cli-and-age.md`,
"Resolved operator decisions"):

> **Web dashboard access:** open reads on the LAN, token-gated writes.

Concretely:

- **Every `GET` is unauthenticated.** The dashboard's pages (`/`,
  `/location/<id>`) and JSON endpoints (`/api/locations`, `/api/latest`,
  `/api/history`) never check a token. Anyone who can reach the bound host
  and port can browse every location's current readings, staleness, and
  history. The default bind, `0.0.0.0:8323`, makes that "anyone on the LAN",
  not just this machine — bind `127.0.0.1:8323` instead if that is not
  acceptable on your network.
- **Every `POST` is token-gated.** The control endpoints (`/control` for the
  HTML form, `/api/set` for JSON clients) require a token, checked with
  `hmac.compare_digest` (constant-time, so a timing side-channel can't leak
  it a byte at a time). The token travels as the `token` form field or the
  `X-Sensibo-Token` header. A missing or wrong token gets HTTP 401 — zero
  writes, not even a diff computed against the real pod state.
- **The dry-run/apply contract still applies on top of the token.** A
  correctly authenticated control POST without an explicit confirm renders
  the same zero-write dry-run diff `sensibo set` would print — it reads the
  pod's current `acState` to compute the diff, but issues no write. Only a
  second, explicit POST carrying `confirm=1` (the "Confirm and apply" button
  on the dry-run page) calls through to `SensiboClient` and writes. This
  reuses `sensibo set`'s own `_process_pod` function directly, so the
  dashboard's control form and the CLI's `set` verb can never silently drift
  on what counts as a write.

## The token file

- **Location:** `~/.sensibo/web-token` by default; override with
  `--token-file PATH`.
- **Generated once, on first run**, with `secrets.token_hex(32)` (64 hex
  characters of cryptographic randomness); a later run reuses whatever is
  already on disk, so restarting `sensibo web` does not invalidate a token
  you've already copied somewhere.
- **Mode `0600`** — readable only by the owner, matching
  `~/.sensibo/.env`'s permissions for the Sensibo API key.
- **The path is logged to stderr on every start; the value never is** —
  neither to stdout, stderr, nor any HTTP response body other than the one
  legitimate read (an operator opening the file themselves).

## The offline property

Every `GET` handler — the dashboard pages and the `/api/*` JSON
endpoints — reads exclusively from the local sqlite store
(`sensibo/store/`, populated by `sensibo collect`). None of them construct a
`SensiboClient` or otherwise touch the network. That means:

- **The dashboard works with the Sensibo cloud completely unreachable.** If
  your internet connection drops, or Sensibo's API is down, every page and
  read-only endpoint keeps serving whatever the local store already has.
- **Only the control form's write path needs the cloud.** `_process_pod`
  reads the pod's current `acState` (a real API call) to compute the dry-run
  diff, and — only after an explicit confirm — writes the change. If the
  cloud is unreachable, the control form's preview/apply steps fail (with the
  underlying `ApiError`'s message surfaced as an HTTP 502), but every other
  page keeps working.

This mirrors the project's core "locally" claim
([`docs/sensibo-api.md`](sensibo-api.md), [`README.md`](../README.md)):
Sensibo itself is cloud-only, so "the dashboard works offline" means *reading
what's already landed locally*, not a LAN-local Sensibo protocol that doesn't
exist.

## Routes

| Path | Method | Auth | What |
|---|---|---|---|
| `/` | GET | open | Every location: latest readings, health status, staleness; collector heartbeat; report list |
| `/location/<id>` | GET | open | One location: latest readings, history sparklines, health status, control form (pods only) |
| `/api/locations` | GET | open | JSON: every location (mirrors `sensibo query locations`) |
| `/api/latest` | GET | open | JSON: latest reading(s); `?location=&field=` |
| `/api/history` | GET | open | JSON: a field's time series; `?location=&field=[&since=][&until=]` |
| `/reports/<name>` | GET | open | One offline `*.svg` report from the reports directory |
| `/control` | POST | token | HTML control result: dry-run diff, or applied result with `confirm=1` |
| `/api/set` | POST | token | Same contract as `/control`, JSON in and out |

`/api/set` and `/control` both accept `pod_id`, `power`, `mode`, `target`,
`fan`, `swing`, and `confirm` — the same fields `sensibo set` accepts as
flags, mapped onto the same `acState` properties.

## Staleness: one source of truth (task t9)

The STALE flag on every page and JSON endpoint above, `sensibo room list`'s
STALE flag, and the MCP `room_list` tool's `stale` field are now all derived
from the same place: `sensibo.health.model.HealthConfig.down_after_seconds`
(default 900s), loaded via `HealthConfig.from_env()` — so an operator's
`SENSIBO_HEALTH_DOWN_AFTER` override changes every one of them together, not
just the health alert evaluator. `WebServer.__init__`'s `stale_after_hours`
parameter still accepts an explicit override (tests use this for
determinism); left unset, it resolves from the environment at construction
time.

When a location has a row in the health table (populated by the health
evaluator once it has run — see the sensor-health-alerts plan), the dashboard
and `/api/locations` show that row's own `status` (`ok` / `down` / `unknown`
/ `unknown_parent_down`), `since`, and `last_ok` instead of just the STALE
flag. A location with no health row yet falls back to the plain derived STALE
flag, exactly as before task t9.

The index page's header also shows the collector's own heartbeat —
`last_cycle_at` / `last_cycle_outcome`, two store-level facts `sensibo
collect` writes every poll cycle — so "no alerts firing" can be told apart
from "the collector stopped running".

## Reports directory (task t9)

`sensibo web` serves offline `*.svg` reports (`sensibo.report.render_report`)
read-only under `/reports/<name>`, listed on the index page newest first.
Resolution order: the `reports_dir` argument to `WebServer.__init__`, then
`SENSIBO_REPORTS_DIR`, then `~/.sensibo/reports/`. A missing directory is an
empty list, not an error. Requests are validated against path traversal and
non-`.svg` names (both 404) — same open-read trust model as every other GET
route above.

## See also

- [`docs/architecture.md`](architecture.md) — the CLI skeleton and the two
  load-bearing contracts (error/stream) every verb, including `web`, honors
  for its own CLI wiring.
- [`docs/api.md`](api.md) — the Python library surface `_process_pod` and
  the dashboard's read paths are built on.
- `sensibo explain web` — the same summary from the CLI itself.
