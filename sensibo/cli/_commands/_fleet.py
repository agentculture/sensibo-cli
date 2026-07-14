"""Pure data-shaping over a ``fleet_snapshot()`` payload — no network, no CLI I/O.

Shared by ``sensibo devices`` and ``sensibo read``, both of which are built on
exactly one :meth:`SensiboClient.fleet_snapshot` call
(``GET /users/me/pods?fields=*``, ``docs/sensibo-api.md``, "Poll with one
call, not one per device"). Never loop per-device against the API here.

Two location kinds, matching ``sensibo/store``'s vocabulary
(``KIND_POD`` / ``KIND_ROOM_SENSOR``):

* a **pod** is a real Sensibo device; its sensor fields are whatever keys are
  present in its own ``measurements`` object — never a hardcoded schema
  (``docs/sensibo-api.md``, "Per-model sensor sets").
* a **Room Sensor is not a pod** — it is a BLE satellite with no pod id of its
  own, nested inside its parent's ``motionSensors[]`` with a stable ``ms_*``
  id and its own ``measurements`` (``docs/sensibo-api.md``, "Trap 2").

``last_seen`` is the location's own ``measurements.time.time`` stamp when the
payload carries one — NOT the poll instant. A Room Sensor that died months ago
still appears in every poll, carrying its old timestamp; stamping the poll
instant would make a dead sensor look alive forever (caught against the
operator's real fleet: a sensor silent since February read as fresh). The poll
instant is only the fallback when a measurements object has no time stamp, and
``None`` means the location reported nothing at all in this snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field

KIND_POD = "pod"
KIND_ROOM_SENSOR = "room_sensor"


@dataclass(frozen=True)
class Location:
    """One sensing location: a pod, or a Room Sensor nested under one."""

    id: str | None
    kind: str
    product_model: str | None
    connection_status: str
    readings: dict[str, object]
    last_seen: str | None
    room: str | None = None
    parent_pod_id: str | None = None
    room_sensors: tuple["Location", ...] = field(default_factory=tuple)

    @property
    def fields(self) -> list[str]:
        return sorted(self.readings)


def pods_from_payload(payload: object) -> list[dict]:
    """Extract the pod list out of a ``fleet_snapshot()`` payload, defensively.

    ``fleet_snapshot()`` returns ``{"result": [pod, ...]}``. Anything that
    doesn't match that shape yields an empty list rather than raising — a
    malformed cloud response is a data problem for the caller to notice via an
    empty fleet, not a crash here.
    """
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if not isinstance(result, list):
        return []
    return [pod for pod in result if isinstance(pod, dict)]


def _room_name(pod: dict) -> str | None:
    room = pod.get("room")
    if isinstance(room, dict):
        name = room.get("name")
        if isinstance(name, str):
            return name
    return None


def _connection_label(status: object) -> str:
    if isinstance(status, dict):
        alive = status.get("isAlive")
        if alive is True:
            return "online"
        if alive is False:
            return "offline"
    return "unknown"


def _readings_of(measurements: object) -> dict[str, object]:
    return dict(measurements) if isinstance(measurements, dict) else {}


def _last_seen(readings: dict[str, object], as_of: str) -> str | None:
    """The location's own latest measurement time, not the poll instant.

    Sensibo stamps each measurements object with ``time.time``. A sensor that
    died months ago still appears in every poll carrying its *old* timestamp —
    using the poll instant here would make a dead sensor look alive forever.
    """
    if not readings:
        return None
    stamp = readings.get("time")
    if isinstance(stamp, dict):
        when = stamp.get("time")
        if isinstance(when, str) and when:
            return when
    return as_of


def _describe_room_sensor(sensor: dict, parent_pod_id: str | None, as_of: str) -> Location:
    readings = _readings_of(sensor.get("measurements"))
    sensor_id = sensor.get("id")
    return Location(
        id=sensor_id if isinstance(sensor_id, str) else None,
        kind=KIND_ROOM_SENSOR,
        product_model=sensor.get("productModel"),
        connection_status=_connection_label(sensor.get("connectionStatus")),
        readings=readings,
        last_seen=_last_seen(readings, as_of),
        parent_pod_id=sensor.get("parentDeviceUid", parent_pod_id),
    )


def _describe_pod(pod: dict, as_of: str) -> Location:
    readings = _readings_of(pod.get("measurements"))
    pod_id = pod.get("id")
    raw_sensors = pod.get("motionSensors")
    sensors = raw_sensors if isinstance(raw_sensors, list) else []
    return Location(
        id=pod_id if isinstance(pod_id, str) else None,
        kind=KIND_POD,
        product_model=pod.get("productModel"),
        connection_status=_connection_label(pod.get("connectionStatus")),
        readings=readings,
        last_seen=_last_seen(readings, as_of),
        room=_room_name(pod),
        room_sensors=tuple(
            _describe_room_sensor(sensor, pod_id, as_of)
            for sensor in sensors
            if isinstance(sensor, dict)
        ),
    )


def describe_fleet(payload: object, as_of: str) -> list[Location]:
    """Every sensing location in ``payload``: one :class:`Location` per pod.

    Each pod's Room Sensors are nested on ``Location.room_sensors`` — they are
    never returned as top-level locations, matching "Room Sensor is not a
    pod" (they have no independent API presence to list them by).
    """
    return [_describe_pod(pod, as_of) for pod in pods_from_payload(payload)]


def find_location(payload: object, location_id: str, as_of: str) -> Location | None:
    """Find one location (pod or nested Room Sensor) by its stable id.

    Searches pods first, then each pod's nested Room Sensors — a ``ms_*`` id
    resolves to the Room Sensor itself, not its parent pod.
    """
    for pod in describe_fleet(payload, as_of):
        if pod.id == location_id:
            return pod
        for sensor in pod.room_sensors:
            if sensor.id == location_id:
                return sensor
    return None
