"""``sensibo devices`` — list the fleet from one ``fleet_snapshot()`` call.

Per pod: its id, ``productModel``, Sensibo room name, connection status, and
the sensor field *names* it actually reports (derived from the keys present
in its ``measurements`` object — never a hardcoded schema,
``docs/sensibo-api.md``, "Per-model sensor sets"). Room Sensors are **not**
pods (``docs/sensibo-api.md``, "Trap 2"): they are listed nested under their
parent pod as sensing locations, with their own fields and a derived
``lastSeen`` (see :mod:`sensibo.cli._commands._fleet` for why that's derived
rather than read off the API).

Read-only. Exactly one HTTP call regardless of fleet size — see
:meth:`sensibo.api.SensiboClient.fleet_snapshot`.
"""

from __future__ import annotations

import argparse
import datetime

from sensibo.cli._commands import _client, _fleet
from sensibo.cli._commands._fleet import Location
from sensibo.cli._output import emit_result


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _room_sensor_json(loc: Location) -> dict[str, object]:
    return {
        "id": loc.id,
        "kind": loc.kind,
        "productModel": loc.product_model,
        "parentDeviceUid": loc.parent_pod_id,
        "connectionStatus": loc.connection_status,
        "fields": loc.fields,
        "lastSeen": loc.last_seen,
    }


def _device_json(loc: Location) -> dict[str, object]:
    return {
        "id": loc.id,
        "kind": loc.kind,
        "productModel": loc.product_model,
        "room": loc.room,
        "connectionStatus": loc.connection_status,
        "fields": loc.fields,
        "roomSensors": [_room_sensor_json(sensor) for sensor in loc.room_sensors],
    }


def fetch_devices() -> tuple[str, list[Location]]:
    """One fleet poll, shaped into :class:`Location` objects. The one seam devices/read share."""
    client = _client.build_client()
    as_of = _now_iso()
    payload = _client.call(client.fleet_snapshot)
    return as_of, _fleet.describe_fleet(payload, as_of)


def _render_text(as_of: str, devices: list[Location]) -> str:
    if not devices:
        return f"as of {as_of}: no pods found on this account."
    lines = [f"{len(devices)} pod(s) as of {as_of}"]
    for pod in devices:
        lines.append("")
        lines.append(
            f"pod {pod.id} ({pod.product_model}) — {pod.room or 'unnamed room'} — "
            f"{pod.connection_status}"
        )
        lines.append(f"  fields: {', '.join(pod.fields) or '(none reported)'}")
        for sensor in pod.room_sensors:
            lines.append(
                f"  room sensor {sensor.id} ({sensor.product_model}) — "
                f"{sensor.connection_status} — last seen: {sensor.last_seen or 'unknown'}"
            )
            lines.append(f"    fields: {', '.join(sensor.fields) or '(none reported)'}")
    return "\n".join(lines)


def cmd_devices(args: argparse.Namespace) -> int:
    as_of, devices = fetch_devices()
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(
            {"asOf": as_of, "devices": [_device_json(pod) for pod in devices]},
            json_mode=True,
        )
    else:
        emit_result(_render_text(as_of, devices), json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "devices",
        help="List the fleet: pods and their nested Room Sensors, one API call.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_devices)
