# Python API

`sensibo-cli` is a CLI first, but the CLI is a thin shell over a plain Python
library. Anything bigger than a shell script — a bigger app, a notebook, an
existing automation daemon — can skip the CLI entirely and just `import
sensibo`. This page is the reference for that surface: what it exports, what
each call does, and the two caveats (the history window, and writes being
immediate) that a script must know before it drives a real air conditioner.

> **Unofficial community tool.** Sensibo is a trademark of Sensibo Ltd. This
> project is not affiliated with, endorsed by, or supported by them.

Prefer an MCP client (Claude Code, Claude Desktop, ...) over a raw Python
import? See [`docs/mcp.md`](mcp.md) — `sensibo mcp serve`, behind the
optional `sensibo-cli[mcp]` extra, exposes the same read/control surface as
MCP tools instead.

## Import weight: `import sensibo` stays light

`import sensibo` re-exports `sensibo.api` (the HTTP client, key resolution,
the error family) and `sensibo.store` (the local time-series store). Neither
of those packages imports `argparse` or `sensibo.cli` — a bare `import
sensibo` never pulls in the CLI's parser, dispatch, or error-formatting layer.
`sensibo.cli` only loads if you explicitly `import sensibo.cli` yourself, or
run the installed `sensibo` console script.

That split is deliberate and tested (`tests/test_public_api.py`, in a fresh
subprocess): a third-party script that only calls the library never pays for
argparse, and never needs to know the CLI's dry-run/`--apply` machinery
exists.

## Quickstart

```python
from sensibo import Client, resolve_api_key

# Resolves SENSIBO_API_KEY from the environment first, then ~/.sensibo/.env.
# Raises MissingApiKeyError if neither has a key.
api_key = resolve_api_key()

client = Client(api_key=api_key)  # sensibo.Client is an alias for sensibo.SensiboClient

# ONE call lists every pod on the account, each with its current measurements
# embedded — never loop this per device (see "Poll with one call" below).
pods = client.get_pods()
pod_id = pods["result"][0]["id"]

# The same pod's live sensor readings, on demand.
measurements = client.get_measurements(pod_id)
print(measurements)  # {"temperature": 21.5, "humidity": 44, ...}

# Set AC state. See "Writes are immediate at this layer" below before you call this.
client.post_ac_states(pod_id, {"on": True, "mode": "cool", "targetTemperature": 24})
```

`Client` (an alias for `SensiboClient`) is the only object you construct.
Every method on it is a thin, one-endpoint wrapper — no verb logic, no
dry-run/apply state, nothing hidden. It owns:

- key resolution and injection (the key travels as the `apiKey` query
  parameter, never a header — see "Key safety" below),
- `Accept-Encoding: gzip` on every request, decoded transparently,
- HTTP 429 backoff with jitter, bounded by `max_retries`,
- client-side pacing between outbound calls (`min_interval`, default 1.5s),
- scrubbing the key out of any exception, log line, or `repr()` it can reach.

See [`docs/sensibo-api.md`](sensibo-api.md) for the full endpoint reference
this client wraps, and [`docs/architecture.md`](architecture.md) for how
`sensibo/api/` and `sensibo/store/` are laid out.

## Poll with one call, not one per device

`get_pods()` (aliased as `client.fleet_snapshot()`) is `GET
/users/me/pods?fields=*` — one HTTP call that embeds every pod's current
measurements. A fleet-wide read is always one call:

```python
fleet = client.fleet_snapshot()
for pod in fleet["result"]:
    print(pod["id"], pod.get("measurements"))
```

Looping `get_pod(pod_id)` once per device for a fleet-wide snapshot blows the
(real, but unpublished) rate limit — don't.

## Writes are immediate at this layer

**`sensibo.Client` has no dry-run contract.** `post_ac_states`,
`patch_ac_state`, `put_smartmode`, `put_timer`, and every other write method
sends its request the instant you call it — there is no preview step, no
`--apply` flag, nothing to confirm first. That safety contract exists one
layer up, in the CLI (`sensibo`'s write verbs preview by default and only act
on `--apply` — see [`docs/architecture.md`](architecture.md)). A script built
directly on this library is responsible for its own preview/confirm step if
it wants one; the library will not add it for you.

```python
# This turns the AC on right now. There is no dry-run at this layer.
client.post_ac_states(pod_id, {"on": True, "mode": "cool", "targetTemperature": 24})

# The single-property variant needs the current state to diff against —
# still immediate, still no preview.
current = client.get_pod(pod_id, fields="acState")["acState"]
client.patch_ac_state(pod_id, "targetTemperature", current, 23)
```

## Reading history: the `days=1` gated window

`get_historical_measurements(pod_id, days=1)` wraps `GET
/pods/{id}/historicalMeasurements?days=...`. `days` defaults to `1`.
Sensibo does not document a maximum, but **empirically, on a non-Plus
account, any `days` value of 2 or more returns HTTP 403** — the accessible
window was exactly one day when this was probed against a real fleet (see
"History retention" in [`docs/sensibo-api.md`](sensibo-api.md)). A paid tier
may raise that limit; this client makes no assumption about which tier the
caller is on.

The client turns that specific 403 into a typed exception instead of a
generic HTTP error, so a caller can catch it and step the window down:

```python
from sensibo import GatedHistoryWindowError

try:
    history = client.get_historical_measurements(pod_id, days=7)
except GatedHistoryWindowError as err:
    print(err.pod_id, err.days, err.remediation)
    history = client.get_historical_measurements(pod_id, days=1)  # the safe default
```

Any other non-403 failure (or a 403 that the client did not specifically
recognise as this gate) raises the plain `HttpError` described below, not
`GatedHistoryWindowError`.

## Handling errors

Every failure `sensibo.api` raises is an `ApiError` — a dataclass carrying
`code`, `message`, and `remediation`. The concrete subclasses:

| Exception | When | Extra attributes |
|---|---|---|
| `MissingApiKeyError` | `resolve_api_key()` (or `Client()` with no key given) found no key in `SENSIBO_API_KEY` or `~/.sensibo/.env` | — |
| `HttpError` | any other non-2xx HTTP response | `status` |
| `RateLimitExceededError` | HTTP 429 retries exhausted | `status` (always `429`) |
| `GatedHistoryWindowError` | `historicalMeasurements` 403'd for the requested `days` | `pod_id`, `days`, `status` (always `403`) |

```python
from sensibo import ApiError, Client, MissingApiKeyError

try:
    client = Client()
except MissingApiKeyError as err:
    # err.remediation names the env var and the ~/.sensibo/.env path to fix it.
    print(f"{err.message}\nhint: {err.remediation}")
    raise SystemExit(1)

try:
    client.get_pods()
except ApiError as err:
    # every ApiError subclass is also just an ApiError — one except catches all of them.
    print(err.to_dict())
```

## Local store: keeping readings past Sensibo's own retention

`sensibo.Store` is the local time-series layer: a plain sqlite3 file the
operator owns, queryable offline, retained for `DEFAULT_RETENTION_DAYS` (two
years) by default. It is independent of the API client — nothing in
`sensibo.store` makes a network call — so it works equally well fed from
`Client` calls, a cron job, or a test fixture.

```python
from sensibo import Client, Store, resolve_api_key

client = Client(api_key=resolve_api_key())
fleet = client.fleet_snapshot()

with Store() as store:  # ~/.sensibo/sensibo.db, unless SENSIBO_DB is set
    for pod in fleet["result"]:
        pod_id = pod["id"]
        store.upsert_location(
            pod_id,
            kind="pod",
            product_model=pod.get("productModel"),
        )
        store.record_readings(pod_id, pod.get("measurements", {}))

    latest = store.latest_readings(pod_id)
    print(latest["temperature"].value, latest["temperature"].unit)
```

`record_readings` branches the `pm25` unit tag on `product_model` internally
(`derive_unit`) — the Pure-vs-Elements polymorphism trap documented in
[`docs/sensibo-api.md`](sensibo-api.md) is handled for you as long as you
pass `product_model` (directly, or via a prior `upsert_location` call for the
same location, as above).

A Room Sensor is not a pod — it has no pod id of its own and surfaces nested
inside its parent Air/Air Pro's `motionSensors[]`. Store it with
`kind="room_sensor"` (`sensibo.KIND_ROOM_SENSOR`) and `parent_pod_id` set to
the pod it's nested under.

## Key safety

- The key resolves as `SENSIBO_API_KEY` in the environment first, then
  `~/.sensibo/.env` (a `KEY=VALUE` line, mode `0600`). `resolve_api_key()`
  implements exactly this precedence and is what `Client()` calls internally
  when you don't pass `api_key` yourself.
- The key is **never** read from a committed file, and never printed,
  returned, or embedded in `repr(client)` — `SensiboClient.__repr__` only
  ever shows the base URL.
- The Sensibo API takes the key as the `apiKey` **query parameter**, not a
  header. That means the raw key lives in every URL this client builds.
  **Never log a URL from this client without scrubbing it first.** Every
  exception, log line, and `repr()` the client itself produces is already
  scrubbed; if your own code captures a URL independently (e.g. from a
  custom logging handler or a proxy), scrub it yourself:

```python
from sensibo import scrub_text, scrub_url

safe_url = scrub_url("https://home.sensibo.com/api/v2/users/me/pods?apiKey=SECRET")
# -> "https://home.sensibo.com/api/v2/users/me/pods?apiKey=REDACTED"

safe_message = scrub_text(f"request failed: {some_error}", api_key=api_key)
```

## Full re-exported surface

Everything below is importable directly from `sensibo` (`from sensibo import
...`), and is also reachable from its originating submodule
(`sensibo.api`/`sensibo.store`) if you prefer the fully-qualified path.

| Name | What it is |
|---|---|
| `SensiboClient`, `Client` | the HTTP client (`Client` is an alias) |
| `DEFAULT_BASE_URL` | `https://home.sensibo.com/api/v2` |
| `resolve_api_key`, `ENV_VAR` | key resolution, and the `SENSIBO_API_KEY` env var name |
| `ApiError` | the base exception; every other error below subclasses it |
| `MissingApiKeyError`, `HttpError`, `RateLimitExceededError`, `GatedHistoryWindowError` | the concrete error types |
| `ERROR_AUTH`, `ERROR_NETWORK`, `ERROR_RATE_LIMIT`, `ERROR_GATED` | the `ApiError.code` values those errors carry |
| `scrub_url`, `scrub_text` | strip the `apiKey` query parameter out of a URL or free text |
| `Store` | the local sqlite time-series store |
| `LocationRecord`, `ReadingRecord` | the two row shapes `Store` returns |
| `DEFAULT_RETENTION_DAYS` | the retention floor `Store.prune()` defaults to (730 days) |
| `KIND_POD`, `KIND_ROOM_SENSOR` | the two `LocationRecord.kind` values |
| `default_db_path`, `resolve_db_path` | where the store's db file lives absent an override |
| `derive_unit` | the unit-tagging rule `Store` uses internally (exposed for callers who record readings by hand) |

Every name above is verified against the real package by
`tests/test_public_api.py::test_every_symbol_documented_in_docs_api_md_actually_exists`
— if this doc ever drifts from what `sensibo/__init__.py` actually exports,
that test fails.
