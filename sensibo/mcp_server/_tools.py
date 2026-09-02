"""Tool implementations backing the MCP server (task t11).

Plain functions — no argparse, no :class:`~sensibo.cli._errors.CliError`, no
stdout/stderr routing. :mod:`sensibo.mcp_server` (the package ``__init__``)
wires these onto the ``mcp`` SDK, which owns the wire format and error
surface; this module owns only the behaviour, so it is trivially testable
without any MCP machinery at all — call a function, assert on the dict it
returns.

This package sits beside :mod:`sensibo.cli`, not on top of it (the same
layering :mod:`sensibo.api` and :mod:`sensibo.store` already use, per
``docs/architecture.md``, "Where the Sensibo code goes"): it depends on
``sensibo.api`` and ``sensibo.store`` directly, and reuses the CLI's pure,
CLI-independent fleet-shaping helper (:mod:`sensibo.cli._commands._fleet` —
no argparse, no CliError, "no network, no CLI I/O" per its own docstring) so
the shape of a fleet snapshot never drifts between the CLI and this surface.
It does not import :mod:`sensibo.cli._errors` or anything else CLI-specific.

Six tool functions live here (five wired onto the MCP server by
:mod:`sensibo.mcp_server`'s ``build_server``, plus :func:`sensibo_health`
added in task t9 — see that function's docstring for why it is not yet
wired), each mirroring one CLI verb's exact behaviour so the MCP surface and
the CLI surface never give two different answers to the same request:

* :func:`list_devices` mirrors ``sensibo devices`` — one
  :meth:`~sensibo.api.SensiboClient.fleet_snapshot` call.
* :func:`read_location` mirrors ``sensibo read``, extended to also accept an
  operator alias or Sensibo room name (resolved against the local store
  first, via :mod:`sensibo.store.rooms`).
* :func:`query_history` mirrors ``sensibo query`` — **local store only**,
  never touches the network.
* :func:`set_ac_state` mirrors ``sensibo set`` — same one-write-call
  contract (one changed field -> ``patch_ac_state``; two or more ->
  ``post_ac_states``), and the same safety property: ``apply`` defaults to
  ``False``, and a dry run issues zero write calls.
* :func:`room_list` mirrors ``sensibo room list``. As of task t9, its
  staleness threshold defaults to :class:`~sensibo.health.model.HealthConfig`
  (loaded via ``HealthConfig.from_env()``), and each row also carries the
  health table's ``health_status``/``health_since``/``health_last_ok`` when a
  row exists.
* :func:`sensibo_health` (task t9) mirrors the local store only, never the
  network: every location's current health row plus transitions since an
  optional ISO 8601 timestamp — what ``sensibo query health --json`` will
  show once that CLI verb lands.

``SensiboClient`` is imported at module scope as the test seam — tests
monkeypatch ``sensibo.mcp_server._tools.SensiboClient`` to a fake, exactly
like ``sensibo.cli._commands.set`` does for the CLI's own ``set`` tests.
``Store`` accepts an explicit ``db`` path everywhere (else ``SENSIBO_DB``,
else ``~/.sensibo/sensibo.db`` — the store's own resolution order): tests
always pass a ``tmp_path`` db, never the real one.
"""

from __future__ import annotations

import datetime
from typing import Any

from sensibo.api import ApiError, SensiboClient
from sensibo.cli._commands import _fleet
from sensibo.health.model import STATUS_OK, HealthConfig
from sensibo.store import (
    HealthRecord,
    LocationRecord,
    LocationResolutionError,
    ReadingRecord,
    Store,
    TransitionRecord,
    is_stale,
    resolve_location,
)

# acState field a `set_ac_state` keyword argument maps onto (mirrors
# sensibo.cli._commands.set._FLAG_TO_FIELD, kept in sync deliberately).
_FIELD_FOR = {
    "power": "on",
    "mode": "mode",
    "target": "targetTemperature",
    "fan": "fanLevel",
    "swing": "swing",
}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _build_client() -> SensiboClient:
    """Construct the real client. Tests monkeypatch this module's ``SensiboClient``."""
    return SensiboClient()


def _unwrap(response: object) -> object:
    """Sensibo wraps payloads as ``{"result": ...}``; unwrap when present."""
    if isinstance(response, dict) and "result" in response:
        return response["result"]
    return response


def _reraise_api_error(err: ApiError) -> None:
    """Turn an :class:`~sensibo.api.ApiError` into a plain, MCP-friendly exception.

    ``sensibo.mcp_server`` never raises ``CliError`` (that contract belongs to
    ``sensibo.cli`` only) — a plain exception with the remediation folded into
    the message is what an MCP tool caller actually sees.
    """
    hint = f" (hint: {err.remediation})" if err.remediation else ""
    raise RuntimeError(f"{err.message}{hint}") from err


def _parse_iso8601(value: str, flag: str) -> float:
    """Parse an ISO 8601 timestamp into epoch seconds (UTC). Mirrors ``sensibo query``."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError as err:
        raise ValueError(f"invalid {flag} timestamp: {value!r} ({err})") from err
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


# -- list_devices -------------------------------------------------------------


def _room_sensor_payload(loc: _fleet.Location) -> dict[str, Any]:
    return {
        "id": loc.id,
        "kind": loc.kind,
        "product_model": loc.product_model,
        "parent_pod_id": loc.parent_pod_id,
        "connection_status": loc.connection_status,
        "fields": loc.fields,
        "last_seen": loc.last_seen,
    }


def _device_payload(loc: _fleet.Location) -> dict[str, Any]:
    return {
        "id": loc.id,
        "kind": loc.kind,
        "product_model": loc.product_model,
        "room": loc.room,
        "connection_status": loc.connection_status,
        "fields": loc.fields,
        "room_sensors": [_room_sensor_payload(sensor) for sensor in loc.room_sensors],
    }


def list_devices() -> dict[str, Any]:
    """List the fleet: every pod and its nested Room Sensors, from one API call.

    Mirrors ``sensibo devices``: per pod, its id, product model, Sensibo room
    name, connection status, and the sensor field *names* it actually reports
    (derived from its own measurements — never a hardcoded schema, since
    sensor sets differ per model). Each pod's Room Sensors are nested under it
    with their own fields — a Room Sensor is not a pod (it has no pod id of
    its own; it only ever surfaces inside its parent's ``motionSensors``).
    Read-only.
    """
    client = _build_client()
    as_of = _now_iso()
    try:
        payload = client.fleet_snapshot()
    except ApiError as err:
        _reraise_api_error(err)
    devices = _fleet.describe_fleet(payload, as_of)
    return {"as_of": as_of, "devices": [_device_payload(loc) for loc in devices]}


# -- read_location --------------------------------------------------------------


def _location_payload(loc: _fleet.Location) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": loc.id,
        "kind": loc.kind,
        "product_model": loc.product_model,
        "connection_status": loc.connection_status,
        "readings": loc.readings,
    }
    if loc.kind == _fleet.KIND_POD:
        payload["room"] = loc.room
        payload["room_sensors"] = [_location_payload(sensor) for sensor in loc.room_sensors]
    else:
        payload["parent_pod_id"] = loc.parent_pod_id
    return payload


def _resolve_alias(location_id: str, *, db: str | None) -> str:
    """Best-effort: translate a store alias/room name to its stable id.

    Falls back to ``location_id`` unchanged whenever the local store has
    nothing to resolve it to (empty store, or no match at all) — the caller
    then tries ``location_id`` verbatim against the live fleet, so a plain
    pod/Room-Sensor id keeps working exactly as it did before alias support
    existed.
    """
    try:
        with Store(db_path=db) as store:
            return resolve_location(store, location_id).id
    except LocationResolutionError:
        return location_id


def read_location(location_id: str, db: str | None = None) -> dict[str, Any]:
    """One snapshot of every current reading for a pod or Room Sensor.

    ``location_id`` may be a stable id (pod id, or a Room Sensor's ``ms_*``
    id), or an operator alias / Sensibo room name registered via
    ``sensibo room name`` — resolved against the local store first. Mirrors
    ``sensibo read``. Read-only.
    """
    resolved_id = _resolve_alias(location_id, db=db)
    client = _build_client()
    as_of = _now_iso()
    try:
        payload = client.fleet_snapshot()
    except ApiError as err:
        _reraise_api_error(err)
    location = _fleet.find_location(payload, resolved_id, as_of)
    if location is None:
        raise LookupError(
            f"no such pod, Room Sensor, alias, or room name: {location_id!r} "
            "(list valid ids with the list_devices tool)"
        )
    return {"as_of": as_of, **_location_payload(location)}


# -- query_history --------------------------------------------------------------

_QUERY_MODES = ("latest", "range")


def _reading_payload(r: ReadingRecord) -> dict[str, Any]:
    return {
        "location_id": r.location_id,
        "field": r.field,
        "timestamp": r.timestamp,
        "value": r.value,
        "unit": r.unit,
    }


def query_history(
    location: str,
    field: str | None = None,
    mode: str = "latest",
    since: str | None = None,
    until: str | None = None,
    db: str | None = None,
) -> dict[str, Any]:
    """Offline reads from the LOCAL store only. Never touches the network.

    Mirrors ``sensibo query``. ``location`` accepts a stable id, an operator
    alias, or a Sensibo room name (:mod:`sensibo.store.rooms`). ``mode``:

    * ``"latest"`` (the default) — the latest reading for ``field``, or every
      field this location has reported when ``field`` is omitted.
    * ``"range"`` — every reading for ``field`` (required in this mode)
      between ``since``/``until``, both **inclusive**, ISO 8601; either bound
      may be omitted for an open-ended range.
    """
    if mode not in _QUERY_MODES:
        raise ValueError(f"mode must be one of {_QUERY_MODES}, got {mode!r}")

    with Store(db_path=db) as store:
        try:
            loc: LocationRecord = resolve_location(store, location)
        except LocationResolutionError as err:
            raise LookupError(str(err)) from err

        if mode == "range":
            if not field:
                raise ValueError("'field' is required when mode='range'")
            since_ts = _parse_iso8601(since, "since") if since else None
            until_ts = _parse_iso8601(until, "until") if until else None
            rows = store.query_range(loc.id, field, since=since_ts, until=until_ts)
        elif field:
            reading = store.latest_reading(loc.id, field)
            rows = [reading] if reading is not None else []
        else:
            rows = list(store.latest_readings(loc.id).values())

    return {
        "location": location,
        "location_id": loc.id,
        "mode": mode,
        "field": field,
        "readings": [_reading_payload(r) for r in rows],
    }


# -- set_ac_state -----------------------------------------------------------------


def _requested_ac_changes(
    *,
    power: str | None,
    mode: str | None,
    target: int | None,
    fan: str | None,
    swing: str | None,
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if power is not None:
        if power not in ("on", "off"):
            raise ValueError("power must be 'on' or 'off'")
        changes[_FIELD_FOR["power"]] = power == "on"
    if mode is not None:
        changes[_FIELD_FOR["mode"]] = mode
    if target is not None:
        changes[_FIELD_FOR["target"]] = target
    if fan is not None:
        changes[_FIELD_FOR["fan"]] = fan
    if swing is not None:
        changes[_FIELD_FOR["swing"]] = swing
    return changes


def _diff_ac_state(current: dict[str, Any], requested: dict[str, Any]) -> dict[str, dict[str, Any]]:
    diff: dict[str, dict[str, Any]] = {}
    for field_name, new_value in requested.items():
        old_value = current.get(field_name)
        if old_value != new_value:
            diff[field_name] = {"from": old_value, "to": new_value}
    return diff


def set_ac_state(
    pod_id: str,
    power: str | None = None,
    mode: str | None = None,
    target: int | None = None,
    fan: str | None = None,
    swing: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Control an AC's power/mode/target/fan/swing.

    **THIS DRIVES AN AIR CONDITIONER IN SOMEONE'S HOME.** ``apply`` defaults
    to ``False`` — exactly mirroring ``sensibo set`` without ``--apply``:
    reads the pod's current ``acState`` and returns the diff of what *would*
    change; issues zero write requests. Call again with ``apply=True`` to
    commit — a single changed field goes through the safe single-property
    PATCH; two or more changed fields go through the full-state POST. Either
    way the resulting state is read back and returned, never assumed.
    """
    requested = _requested_ac_changes(power=power, mode=mode, target=target, fan=fan, swing=swing)
    if not requested:
        raise ValueError(
            "no fields given to change: pass at least one of power/mode/target/fan/swing"
        )

    client = _build_client()
    try:
        current_response = _unwrap(client.get_pod(pod_id, fields="acState"))
    except ApiError as err:
        _reraise_api_error(err)

    ac_state = current_response.get("acState") if isinstance(current_response, dict) else None
    if not isinstance(ac_state, dict):
        raise LookupError(f"pod {pod_id!r} returned no acState in its response")

    diff = _diff_ac_state(ac_state, requested)
    result: dict[str, Any] = {"pod_id": pod_id, "apply": bool(apply), "changes": diff}
    if not apply or not diff:
        return result

    try:
        if len(diff) == 1:
            ((prop, change),) = diff.items()
            client.patch_ac_state(pod_id, prop, ac_state, change["to"])
            result["method"] = "patch"
        else:
            merged = dict(ac_state)
            for prop, change in diff.items():
                merged[prop] = change["to"]
            client.post_ac_states(pod_id, merged)
            result["method"] = "post"

        result_response = _unwrap(client.get_pod(pod_id, fields="acState"))
    except ApiError as err:
        _reraise_api_error(err)

    result["result_ac_state"] = (
        result_response.get("acState") if isinstance(result_response, dict) else None
    )
    return result


# -- room_list --------------------------------------------------------------------


def _location_summary(
    loc: LocationRecord, *, stale_after_hours: float, health: HealthRecord | None
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "id": loc.id,
        "kind": loc.kind,
        "product_model": loc.product_model,
        "room_name": loc.room_name,
        "alias": loc.alias,
        "last_seen": loc.last_seen,
        "stale": is_stale(loc.last_seen, stale_after_hours=stale_after_hours),
        "health_status": None,
        "health_since": None,
        "health_last_ok": None,
    }
    if health is not None:
        summary["health_status"] = health.status
        summary["health_since"] = health.since
        summary["health_last_ok"] = health.last_ok
    return summary


def room_list(stale_after_hours: float | None = None, db: str | None = None) -> dict[str, Any]:
    """Every known sensing location: id, kind, model, room name, alias, staleness.

    Mirrors ``sensibo room list``. Local store only. A location's ``stale``
    flag is set once it hasn't been seen in more than ``stale_after_hours``,
    which defaults to ``None`` -- resolved at this call, the MCP/CLI boundary,
    from :class:`~sensibo.health.model.HealthConfig.from_env` (task t9's
    single source of truth for staleness) rather than a hardcoded 24h.
    ``health_status``/``health_since``/``health_last_ok`` carry the health
    table's own values when a row exists for that location (``None``
    otherwise -- callers fall back to ``stale``).
    """
    resolved_stale_after_hours = (
        stale_after_hours
        if stale_after_hours is not None
        else HealthConfig.from_env().down_after_seconds / 3600.0
    )
    with Store(db_path=db) as store:
        locations = store.list_locations()
        health_by_id = {h.location_id: h for h in store.list_health()}
    return {
        "stale_after_hours": resolved_stale_after_hours,
        "locations": [
            _location_summary(
                loc,
                stale_after_hours=resolved_stale_after_hours,
                health=health_by_id.get(loc.id),
            )
            for loc in locations
        ],
    }


# -- sensibo_health -----------------------------------------------------------------


def _health_row_payload(health: HealthRecord) -> dict[str, Any]:
    return {
        "location_id": health.location_id,
        "status": health.status,
        "since": health.since,
        "since_iso": _iso_epoch(health.since),
        "last_ok": health.last_ok,
        "last_ok_iso": _iso_epoch(health.last_ok) if health.last_ok is not None else None,
        "parent_pod_id": health.parent_pod_id,
    }


def _iso_epoch(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()


def _transitions_with_durations(transitions: list[TransitionRecord]) -> list[dict[str, Any]]:
    """Attach ``duration_seconds`` to each transition that *closes* an outage.

    A location's outage opens at the transition into a non-``ok`` status and
    closes at the next transition back to ``ok`` for that same location;
    ``duration_seconds`` on the closing transition is the time between the
    two. Open-ended outages (no recovery transition yet) carry
    ``duration_seconds: None``, same as every transition that isn't itself a
    recovery. Assumes ``transitions`` is already ordered oldest-first per
    location, which is what :meth:`Store.list_transitions` guarantees.
    """
    outage_started_at: dict[str, float] = {}
    payloads: list[dict[str, Any]] = []
    for t in transitions:
        duration_seconds: float | None = None
        if t.to_status == STATUS_OK:
            start = outage_started_at.pop(t.location_id, None)
            if start is not None:
                duration_seconds = t.at - start
        else:
            outage_started_at.setdefault(t.location_id, t.at)
        payloads.append(
            {
                "location_id": t.location_id,
                "from_status": t.from_status,
                "to_status": t.to_status,
                "at": t.at,
                "at_iso": _iso_epoch(t.at),
                "duration_seconds": duration_seconds,
            }
        )
    return payloads


def sensibo_health(since: str | None = None, db: str | None = None) -> dict[str, Any]:
    """Every location's current health row, plus transitions since an optional
    ISO 8601 timestamp -- mirroring what ``sensibo query health --json`` shows.

    Local store only, read-only. Each health row carries ``status`` (one of
    ``ok``/``down``/``unknown``/``unknown_parent_down``), ``since``, and
    ``last_ok``. Each transition additionally carries ``duration_seconds``
    when it is the transition that *closed* an outage (a transition back to
    ``ok``); it is ``None`` for every other transition, including an outage
    still open. Also reports the collector's own heartbeat --
    ``last_cycle_at``/``last_cycle_outcome``, written each poll cycle by
    ``sensibo collect`` -- so a client can tell "no alerts" apart from "the
    collector stopped running".
    """
    with Store(db_path=db) as store:
        health_rows = store.list_health()
        since_ts = _parse_iso8601(since, "since") if since else None
        transitions = store.list_transitions(since=since_ts)
        last_cycle_at = store.get_meta("last_cycle_at")
        last_cycle_outcome = store.get_meta("last_cycle_outcome")
    return {
        "as_of": _now_iso(),
        "since": since,
        "last_cycle_at": last_cycle_at,
        "last_cycle_outcome": last_cycle_outcome,
        "locations": [_health_row_payload(h) for h in health_rows],
        "transitions": _transitions_with_durations(transitions),
    }
