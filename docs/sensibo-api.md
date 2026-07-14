# The Sensibo API

The reference this project builds on. Researched 2026-07-14 against Sensibo's
official API documentation at <https://support.sensibo.com/api/> (the
operator-supplied primary source), the official OpenAPI spec, Sensibo's own
Python SDK, the Home Assistant integration, and `pysensibo`.

**Every claim below is tagged with a confidence level.** Where something is
unverified, it says so — do not promote a *likely* to a *confirmed* without
checking against a real API key and real devices. A doc that quietly guesses is
worse than one that admits the gap.

- **CONFIRMED** — read in official docs or in shipping source.
- **LIKELY** — multiple secondary sources agree; no primary source.
- **UNVERIFIED** — could not establish. Treat as unknown.

## The load-bearing question: is there a local API?

**No — Sensibo is cloud-only (CONFIRMED).** There is no local REST endpoint, no
MQTT, and no documented on-network protocol on stock firmware. Any reading or
command this tool issues must round-trip through `home.sensibo.com`.

What establishes it:

| Evidence | Source |
|---|---|
| Home Assistant's Sensibo integration declares `"iot_class": "cloud_polling"`, and its docs state data is polled from the Sensibo API once a minute. HA is rigorous about this field. | [HA integration](https://www.home-assistant.io/integrations/sensibo/) |
| `pysensibo` — the de-facto client, used by HA — hardcodes only `home.sensibo.com` URLs. Zero local/device addresses anywhere in the source. | [pysensibo](https://github.com/andrey-git/pysensibo) |
| Sensibo's official OpenAPI spec declares a single `servers:` entry: `https://home.sensibo.com/api/v2`. No local server is documented. | [OpenAPI spec](http://support.sensibo.com/sensibo.openapi.yaml) |
| The openHAB binding likewise requires an API key and polls the cloud. | [openHAB binding](https://www.openhab.org/addons/bindings/sensibo) |

**MQTT (CONFIRMED negative).** The one MQTT project that exists,
`denwilliams/sensibo-mqtt`, is a *bridge*: it takes an API key, polls the cloud,
and republishes to MQTT. There is no firmware-level MQTT.

**Matter / Thread (UNVERIFIED — assume no).** Sensibo's own Air Pro product page
never mentions Matter or Thread. Two low-quality secondary sites claim support;
no primary source corroborates. **Do not state Matter support anywhere.**

**Community reverse-engineering** only ever arrives at *replacing* the firmware
(reflashing the Sky's ESP8266 with ESPHome), which destroys Sensibo's IR
codebase and the cloud service. That is not a local API — it is a different
product.

### The one real exception: HomeKit on Air Pro

Sensibo **Air Pro** supports native Apple HomeKit pairing, and HomeKit Accessory
Protocol over IP genuinely is a LAN protocol (CONFIRMED — the device announces
itself via mDNS; HA's `homekit_controller` is `local_push`). But it is not a
viable basis for this tool:

- **The control surface is tiny.** Sensibo's own docs: HomeKit gives modes (off,
  heat, cool, auto) and target temperature, and does **not** support fan levels
  or swing. No air-quality sensors, no Climate React, no schedules, no history.
- **Pairing is exclusive.** HAP pairs to exactly one controller — pairing it
  elsewhere unpairs it from Apple Home.
- **The device reportedly still needs internet regardless** (LIKELY — community
  report, not vendor-confirmed).
- **Not available on Sky at all**, which needs cloud-backed Homebridge.

### What this means for the product

"Collect locally" is a claim about **where the data comes to rest**, not about
the transport. We poll the cloud and persist to a store the operator owns and can
query offline. Say that plainly; never imply a local protocol.

The constraint cuts in our favour on retention: since there is no device to fall
back on, a reading that ages out of Sensibo's cloud is *gone* unless we kept it.

## Base URL and auth (CONFIRMED)

- **Base URL:** `https://home.sensibo.com/api/v2`
- **Auth: the API key is a QUERY PARAMETER, not a header** — `?apiKey={key}`.
  Keys are minted at `https://home.sensibo.com/me/api`.
  **Consequence: never log a raw request URL.** Scrub `apiKey` before any
  diagnostic output.
- **Always send `Accept-Encoding: gzip`.** Sensibo documents gzip as a way to
  *raise your rate limit* — so it is a correctness concern, not just bandwidth.
- `/api/v1` still exists and is live; `pysensibo` uses it in production for some
  paths. A `graphql/v1` endpoint constant exists in pysensibo but is never
  called — undocumented, ignore it.
- OAuth2 exists for commercial users only.

## Endpoints (CONFIRMED unless noted)

Relative to `/api/v2`:

| Endpoint | Method | Notes |
|---|---|---|
| `/users/me/pods` | GET | Device list. Takes `fields` (comma-separated, or `*`). **This is the polling workhorse — see below.** |
| `/pods/{id}` | GET | Device detail. Takes `fields`. |
| `/pods/{id}/historicalMeasurements` | GET | Takes `days` (default 1). |
| `/pods/{id}/acStates` | GET, POST | **The control surface.** GET takes `limit` (max 20). POST body is `{"acState": {...}}`. |
| `/pods/{id}/acStates/{property}` | PATCH | Change **one** property: `{"currentAcState": {...}, "newValue": ...}`. The safe way to toggle a single field. |
| `/pods/{id}/smartmode` | GET, PUT, POST | Climate React — Sensibo's own server-side threshold automation. |
| `/pods/{id}/timer/` | GET, PUT, DELETE | **Note the trailing slash.** |
| `/pods/{id}/schedules/` | GET, POST | Trailing slash. Per-schedule ops at `/schedules/{schedule_id}/`. |
| `/pods/{id}/events` | GET | Device events, with a documented event-code taxonomy. |
| `/pods/{id}/measurements` | GET | ⚠️ **Undocumented.** Absent from the current OpenAPI spec, but present in Sensibo's *own* official Python SDK. It works, but **don't build on it** — see below. |

**Known inconsistency (UNVERIFIED which is canonical):** the OpenAPI spec places
`timer/` and `schedules/` under **v2**, but `pysensibo` calls them on **v1** and
Home Assistant ships that in production. Both appear to work. Prefer the
documented v2 path; be ready to fall back.

Several further endpoints (`cleanFiltersNotification`, `calibration/`,
`pureboost`) are undocumented but used in production by HA. They work; treat them
as unstable.

### Poll with one call, not one per device

Do **not** loop `/pods/{id}/measurements` per device. Instead:

```text
GET /users/me/pods?fields=*
```

This embeds each device's full `measurements` object in a **single** response.
It is both the documented path and the rate-limit-safe one — O(1) instead of
O(n) in device count. It is what `pysensibo` and Home Assistant do.

## Rate limits

- **Rate limiting exists and returns HTTP 429 (CONFIRMED, official).** Sensibo
  also reserves the right to restrict or rate-limit any endpoint at any time.
- **The actual numeric limit is published nowhere (UNVERIFIED).** Not in the
  spec, not on the support site, not in any integration's source. Maintainers hit
  it without ever being told the number.
- **It is tight (LIKELY).** openHAB's issue #17090 reports that per-device
  initialization blows the limit at more than about five devices — and the fix
  was exactly the single-call `fields=*` pattern above.

**Design rules:** one `fields=*` call per poll; gzip always on; don't poll faster
than ~60s (Home Assistant's battle-tested floor); back off on 429.

## History retention

- `historicalMeasurements` takes `days`, default 1. **Sensibo documents no
  maximum and no retention policy (CONFIRMED absence).**
- **The window appears to be ~7 days (LIKELY, not confirmed).** The CRAN R client
  `sensibo.sky` documents the parameter as "max 7 days". That is a third-party
  implementation, not a Sensibo statement.
- Don't conflate this with Sensibo Plus's advertised "30 days of event logs" —
  that is the *events* endpoint, not measurements.

> **Action:** this is trivially falsifiable the moment we have an API key —
> request `days=30` and see what comes back. **Do that before putting a number in
> the README.**

## Per-model sensor sets

Sensor sets differ per model. **Design the collector around "whatever fields this
pod reports", never a hardcoded schema**, or readings silently vanish on models
we didn't test.

Keyed by the `productModel` field:

| Model | Reports | Notes |
|---|---|---|
| *all pods* | `temperature`, `humidity`, `feelsLike`, `acState`, `connectionStatus`, `firmwareVersion` | baseline |
| **Sky / Sky Plus** | the baseline. No air quality. | Sky Plus can promote a Room Sensor to main sensor |
| **Air** | temperature, humidity only | no AQ sensors |
| **Air Pro** (`airq`) | + `tvoc` (ppb), `co2` (ppm) | **No PM2.5.** |
| **Elements** (`elements`) | + `pm25` (µg/m³), `tvoc`, `co2`, `etoh`, `iaq` | real particulate sensor |
| **Pure** (`pure`) | `pm25` — **as an AQI enum, not µg/m³** | no timer, no smartmode |
| **Room Sensor** | `motion`, temperature, humidity, battery, rssi | **not a pod** — see below |

### Trap 1: `pm25` is polymorphic 🚨

On **Pure**, the `pm25` JSON key is **not a concentration** — it is an
air-quality *index enum* (`0` unknown, `1` good, `2` moderate, `3` bad). On
**Elements** the same key is µg/m³. `pysensibo` branches on
`productModel == "pure"` to decide (CONFIRMED in source).

**Same key, different units, different meaning.** Store both into one column and
the collected history is corrupt — irreversibly, since there's no local device to
re-read from. Branch on `productModel` before persisting.

### Trap 2: Room Sensor is not a pod

It is a **Bluetooth LE** satellite that talks to a parent Air / Air Pro
(CONFIRMED, official). It has no IP, no pod ID, and no independent API presence.
It surfaces **nested inside the parent pod** as `motionSensors[]`, and the parent
gains a `roomIsOccupied` boolean. It does not work with Sky.

Anything that enumerates "devices" by iterating pods will miss every Room Sensor.

### Trap 3: CO2 is derived, not measured

On both Air Pro and Elements, Sensibo's own product specs say "CO2
**equivalent**" — it is estimated from TVOC, not read from a real NDIR sensor
(CONFIRMED). Elements' `etoh` is likewise an "equivalent". Don't present these as
ground-truth CO2, and be careful about driving automation off them.

**UNVERIFIED:** the `rcda` field is parsed by `pysensibo` for Elements, but its
meaning is documented nowhere. Don't guess it.

## Why we don't depend on `pysensibo`

`pysensibo` **is** the de-facto Python client — Home Assistant's official
integration depends on it at `quality_scale: platinum`, and it's actively (if
slowly) maintained. Read it as a reference, especially for the undocumented
endpoints and the per-model quirks.

But do not take it as a dependency, for two decisive reasons:

1. **It does not implement `historicalMeasurements` — at all.** The single
   endpoint this product's "retain past the vendor window" thesis is built on is
   the one endpoint it doesn't cover. We would depend on it and still hand-roll
   our core call.
2. **It would break the zero-runtime-dependency design.** It hard-requires
   `aiohttp`, pulling in a full async HTTP stack and its transitive deps. Its own
   README says it was written specifically for Home Assistant, not as a
   general-purpose client.

The Sensibo API is plain REST/JSON with the key in a query string. That is on the
order of a hundred lines against stdlib `urllib.request` + `json` + `gzip`. Keep
`dependencies = []` and write our own.

## Open questions to settle against real hardware

The brief was right that a document surviving contact with the hardware unchanged
is a suspicious document. These are the things to check first:

1. **What is the real `historicalMeasurements` retention?** Request `days=30`.
2. **What does the operator's own fleet actually report?** Dump
   `GET /users/me/pods?fields=*` verbatim and compare against the per-model table
   above before committing to a storage schema.
3. **Where do `timer/` and `schedules/` actually live** — v1 or v2?
4. **What is the real rate limit?** Find the ceiling empirically, gently.
