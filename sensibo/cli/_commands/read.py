"""``sensibo read <pod-or-location-id>`` — one snapshot of every current reading.

Built on the same single ``fleet_snapshot()`` call as ``sensibo devices``
(:mod:`sensibo.cli._commands._fleet`). Accepts either a pod id or a nested
Room Sensor's ``ms_*`` id:

* a **pod** id prints every field in that pod's own ``measurements``, plus
  each of its nested Room Sensors' own readings (``motionSensors``,
  ``docs/sensibo-api.md``, "Trap 2" — a Room Sensor has no independent API
  presence, so its readings only ever surface nested under its parent);
* a **Room Sensor** (``ms_*``) id prints just that sensor's own readings.

Read-only. Unknown ids raise :class:`CliError` with a remediation pointing at
``sensibo devices``.
"""

from __future__ import annotations

import argparse
import datetime

from sensibo.cli._commands import _client, _fleet
from sensibo.cli._commands._fleet import Location
from sensibo.cli._errors import EXIT_USER_ERROR, CliError
from sensibo.cli._output import emit_result


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _location_json(loc: Location) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": loc.id,
        "kind": loc.kind,
        "productModel": loc.product_model,
        "connectionStatus": loc.connection_status,
        "readings": loc.readings,
    }
    if loc.kind == _fleet.KIND_POD:
        payload["room"] = loc.room
        payload["motionSensors"] = [_location_json(sensor) for sensor in loc.room_sensors]
    else:
        payload["parentDeviceUid"] = loc.parent_pod_id
    return payload


def _format_readings(readings: dict[str, object], *, indent: str) -> list[str]:
    if not readings:
        return [f"{indent}(no current readings)"]
    return [f"{indent}{field}: {readings[field]}" for field in sorted(readings)]


def _render_text(as_of: str, loc: Location) -> str:
    lines: list[str]
    if loc.kind == _fleet.KIND_POD:
        lines = [
            f"pod {loc.id} ({loc.product_model}) — {loc.room or 'unnamed room'} — "
            f"{loc.connection_status} — as of {as_of}"
        ]
        lines.extend(_format_readings(loc.readings, indent="  "))
        for sensor in loc.room_sensors:
            lines.append(
                f"  room sensor {sensor.id} ({sensor.product_model}) — {sensor.connection_status}"
            )
            lines.extend(_format_readings(sensor.readings, indent="    "))
    else:
        lines = [
            f"room sensor {loc.id} ({loc.product_model}) — parent {loc.parent_pod_id} — "
            f"{loc.connection_status} — as of {as_of}"
        ]
        lines.extend(_format_readings(loc.readings, indent="  "))
    return "\n".join(lines)


def cmd_read(args: argparse.Namespace) -> int:
    client = _client.build_client()
    as_of = _now_iso()
    payload = _client.call(client.fleet_snapshot)
    location = _fleet.find_location(payload, args.location_id, as_of)
    if location is None:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"no such pod or location id: {args.location_id}",
            remediation="list valid ids with: sensibo devices",
        )

    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result({"asOf": as_of, **_location_json(location)}, json_mode=True)
    else:
        emit_result(_render_text(as_of, location), json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "read",
        help="One snapshot of every current reading for a pod or Room Sensor id.",
    )
    p.add_argument(
        "location_id",
        help="A pod id, or a nested Room Sensor's ms_* id (see 'sensibo devices').",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_read)
