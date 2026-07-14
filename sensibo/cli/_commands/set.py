"""``sensibo set`` — the control verb: power, mode, target, fan, swing.

**THIS DRIVES AN AIR CONDITIONER IN SOMEONE'S HOME.** Dry-run by default
(``docs/architecture.md``, "Write verbs: dry-run by default") is this verb's
core safety property, not a nicety:

* **Without ``--apply``** (the default): read the pod's current ``acState``
  and print exactly what *would* change, field by field. Zero write requests
  are issued — proven in ``tests/test_cli_set.py``.
* **With ``--apply``**: a single changed property goes through
  ``SensiboClient.patch_ac_state`` (``PATCH /pods/{id}/acStates/{property}`` —
  the safe single-field toggle); two or more changed properties go through
  ``SensiboClient.post_ac_states`` (``POST /pods/{id}/acStates`` with the full
  merged target state). Either way, the resulting state is read back and
  reported — never assumed.
* **``--all``** applies the same requested change to every pod in the fleet,
  discovered with **one** fleet listing call
  (``SensiboClient.fleet_snapshot`` — ``docs/sensibo-api.md``, "Poll with one
  call, not one per device"). Per-pod PATCH/POST writes still only happen with
  ``--apply``; a dry run against ``--all`` shows the per-pod diff with zero
  HTTP writes.

Flags map onto ``acState`` fields as Sensibo (and ``pysensibo``) define them:
``--power`` -> ``on`` (bool), ``--mode`` -> ``mode``, ``--target`` ->
``targetTemperature``, ``--fan`` -> ``fanLevel``, ``--swing`` -> ``swing``.
"""

from __future__ import annotations

import argparse
from typing import Any

from sensibo.api import ApiError, HttpError, SensiboClient
from sensibo.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from sensibo.cli._output import emit_result

_POWER_CHOICES = ("on", "off")
_MODE_CHOICES = ("cool", "heat", "fan", "dry", "auto")

# CLI flag -> acState field name (docs/sensibo-api.md, "The control surface";
# field names confirmed against pysensibo's own acState usage).
_FLAG_TO_FIELD = {
    "power": "on",
    "mode": "mode",
    "target": "targetTemperature",
    "fan": "fanLevel",
    "swing": "swing",
}


# -- building the requested change set and diffing it against reality --------


def _requested_changes(args: argparse.Namespace) -> dict[str, Any]:
    """The acState fields the caller asked to change, translated from flags."""
    changes: dict[str, Any] = {}
    if args.power is not None:
        changes[_FLAG_TO_FIELD["power"]] = args.power == "on"
    if args.mode is not None:
        changes[_FLAG_TO_FIELD["mode"]] = args.mode
    if args.target is not None:
        changes[_FLAG_TO_FIELD["target"]] = args.target
    if args.fan is not None:
        changes[_FLAG_TO_FIELD["fan"]] = args.fan
    if args.swing is not None:
        changes[_FLAG_TO_FIELD["swing"]] = args.swing
    return changes


def _diff(current: dict[str, Any], requested: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Only the fields where the requested value actually differs from ``current``."""
    diff: dict[str, dict[str, Any]] = {}
    for field, new_value in requested.items():
        old_value = current.get(field)
        if old_value != new_value:
            diff[field] = {"from": old_value, "to": new_value}
    return diff


# -- reads --------------------------------------------------------------------


def _unwrap(response: object) -> object:
    """Sensibo wraps payloads as ``{"result": ...}``; unwrap when present."""
    if isinstance(response, dict) and "result" in response:
        return response["result"]
    return response


def _current_ac_state(client: SensiboClient, pod_id: str) -> dict[str, Any]:
    result = _unwrap(client.get_pod(pod_id, fields="acState"))
    ac_state = result.get("acState") if isinstance(result, dict) else None
    if not isinstance(ac_state, dict):
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"pod {pod_id!r} returned no acState in its response",
            remediation="check the pod id and the Sensibo API status",
        )
    return ac_state


def _fleet_pods(client: SensiboClient) -> list[dict[str, Any]]:
    result = _unwrap(client.fleet_snapshot())
    if not isinstance(result, list):
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="fleet listing returned an unexpected shape (expected a list of pods)",
            remediation="check the Sensibo API status",
        )
    return result


# -- writes ---------------------------------------------------------------


def _apply_changes(
    client: SensiboClient,
    pod_id: str,
    current: dict[str, Any],
    diff: dict[str, dict[str, Any]],
) -> str:
    """Write ``diff`` to ``pod_id``; return which method was used ("patch"/"post")."""
    if len(diff) == 1:
        ((prop, change),) = diff.items()
        client.patch_ac_state(pod_id, prop, current, change["to"])
        return "patch"
    merged = dict(current)
    for prop, change in diff.items():
        merged[prop] = change["to"]
    client.post_ac_states(pod_id, merged)
    return "post"


def _process_pod(
    client: SensiboClient,
    pod_id: str,
    requested: dict[str, Any],
    *,
    apply: bool,
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Diff one pod against ``requested``; write and read back only if ``apply``."""
    if current is None:
        current = _current_ac_state(client, pod_id)
    diff = _diff(current, requested)
    entry: dict[str, Any] = {"pod_id": pod_id, "changes": diff}
    if not apply or not diff:
        return entry
    entry["method"] = _apply_changes(client, pod_id, current, diff)
    entry["result_ac_state"] = _current_ac_state(client, pod_id)
    return entry


# -- error mapping: ApiError -> CliError -------------------------------------

# HTTP statuses that mean "you asked for something invalid" (bad pod id, bad
# body, unauthorized key) rather than "the environment/network is broken".
_USER_ERROR_STATUSES = (400, 401, 403, 404)


def _map_api_error(err: ApiError) -> CliError:
    code = (
        EXIT_USER_ERROR
        if isinstance(err, HttpError) and err.status in _USER_ERROR_STATUSES
        else EXIT_ENV_ERROR
    )
    return CliError(code=code, message=err.message, remediation=err.remediation)


# -- rendering ----------------------------------------------------------------


def _render_pod_text(entry: dict[str, Any], *, apply: bool) -> list[str]:
    pod_id = entry["pod_id"]
    diff = entry["changes"]
    if not diff:
        return [f"{pod_id}: already matches the requested state; nothing to change"]

    lines = [f"{pod_id}: {'applied via ' + entry['method'] if apply else 'would change'}"]
    for field, change in diff.items():
        lines.append(f"  {field}: {change['from']!r} -> {change['to']!r}")

    result_state = entry.get("result_ac_state")
    if apply and result_state:
        lines.append(f"{pod_id}: state after apply")
        for field, value in sorted(result_state.items()):
            lines.append(f"  {field}: {value!r}")
    return lines


def _render_text(payload: dict[str, Any]) -> str:
    apply = bool(payload.get("applied"))
    lines: list[str] = []
    if not apply:
        lines.append("DRY RUN — no changes applied; pass --apply to commit")
    if "pods" in payload:
        for entry in payload["pods"]:
            lines.extend(_render_pod_text(entry, apply=apply))
    else:
        lines.extend(_render_pod_text(payload, apply=apply))
    return "\n".join(lines)


def _emit(payload: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(_render_text(payload), json_mode=False)


# -- the verb -------------------------------------------------------------


def cmd_set(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    requested = _requested_changes(args)
    if not requested:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="no fields given to change",
            remediation="pass at least one of --power/--mode/--target/--fan/--swing",
        )
    if not args.all and not args.pod_id:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="a pod id is required unless --all is given",
            remediation="pass a pod id, or use --all to target every pod in the fleet",
        )

    try:
        client = SensiboClient()
        if args.all:
            pods = _fleet_pods(client)
            results = []
            for pod in pods:
                pod_id = pod.get("id")
                raw_state = pod.get("acState")
                current = raw_state if isinstance(raw_state, dict) else None
                results.append(
                    _process_pod(client, pod_id, requested, apply=args.apply, current=current)
                )
            payload: dict[str, Any] = {"applied": bool(args.apply), "pods": results}
        else:
            result = _process_pod(client, args.pod_id, requested, apply=args.apply)
            payload = {"applied": bool(args.apply), **result}
    except ApiError as err:
        raise _map_api_error(err) from err

    _emit(payload, json_mode=json_mode)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "set",
        help=(
            "Control an AC's power/mode/target/fan/swing. Dry-run by default; "
            "--apply commits the change."
        ),
    )
    p.add_argument(
        "pod_id",
        metavar="pod-id",
        nargs="?",
        help="Pod id to control. Omit when using --all.",
    )
    p.add_argument("--power", choices=_POWER_CHOICES, default=None, help="Turn the AC on or off.")
    p.add_argument("--mode", choices=_MODE_CHOICES, default=None, help="AC operating mode.")
    p.add_argument("--target", type=int, default=None, help="Target temperature.")
    p.add_argument(
        "--fan",
        default=None,
        help="Fan level (device-specific, e.g. quiet/low/medium/high/auto).",
    )
    p.add_argument("--swing", default=None, help="Swing mode (device-specific).")
    p.add_argument(
        "--all",
        action="store_true",
        help="Apply the same requested change to every pod in the fleet.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Commit the change. Without this flag, 'set' is dry-run only and writes nothing.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_set)
