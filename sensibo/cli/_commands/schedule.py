"""``sensibo schedule`` — recurring server-side automation on a pod.

Schedules run **inside Sensibo's cloud** (``docs/sensibo-api.md``:
``/pods/{id}/schedules/`` — note the trailing slash, and per-schedule ops at
``/schedules/{schedule_id}/``): a schedule fires even while this operator's
machine is asleep. Every verb here carries the cloud-execution marker
(:mod:`sensibo.cli._cloud`).

Verbs:

* ``schedule list <pod>`` — read-only, ``GET /pods/{id}/schedules/``.
* ``schedule create <pod> --time HH:MM ...`` — write. Dry-run by default:
  shows the existing schedules and the requested new one, and calls
  nothing. ``--apply`` commits via ``POST /pods/{id}/schedules/``.
* ``schedule delete <pod> <schedule-id>`` — write. Dry-run by default: shows
  the matching existing schedule (if the list includes it) and calls
  nothing. ``--apply`` commits via ``DELETE /pods/{id}/schedules/{id}/``.

Sensibo documents these endpoints' paths and methods but not their request-
body schema. ``create`` builds a reasonable ``{isEnabled, acState, when}``
body from friendly flags; ``--raw-body`` is an escape hatch to send an exact
JSON body when the friendly flags don't match what a real pod expects.
"""

from __future__ import annotations

import argparse
import re

from sensibo.api import ApiError, SensiboClient
from sensibo.cli._commands._automation import (
    build_payload,
    make_overview_command,
    parse_raw_body,
    read_payload,
    render_read_text,
    render_write_text,
)
from sensibo.cli._commands._client import from_api_error
from sensibo.cli._errors import EXIT_USER_ERROR, CliError
from sensibo.cli._output import emit_result

_VERB_LINES = [
    "schedule list <pod> — list the schedules configured on a pod",
    "schedule create <pod> --time HH:MM ... — create a schedule "
    "(dry-run by default; --apply commits)",
    "schedule delete <pod> <schedule-id> — delete a schedule "
    "(dry-run by default; --apply commits)",
]
cmd_overview = make_overview_command("sensibo schedule", _VERB_LINES)

_ALL_DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _client() -> SensiboClient:
    return SensiboClient()


def _validate_time(value: str) -> str:
    if not _TIME_RE.match(value):
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid --time value: {value!r}",
            remediation="use 24-hour HH:MM, e.g. --time 22:30",
        )
    return value


def _parse_days(value: str) -> list[str]:
    if value.strip().lower() == "all":
        return list(_ALL_DAYS)
    days = [d.strip().upper() for d in value.split(",") if d.strip()]
    invalid = [d for d in days if d not in _ALL_DAYS]
    if invalid:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid --days value(s): {invalid}",
            remediation=f"use comma-separated days from {_ALL_DAYS} or 'all'",
        )
    return days


def _build_schedule_body(args: argparse.Namespace) -> dict[str, object]:
    if getattr(args, "raw_body", None):
        return parse_raw_body(args.raw_body)

    if not args.time:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="--time is required unless --raw-body is given",
            remediation="pass --time HH:MM (24-hour), e.g. --time 22:30",
        )
    time_value = _validate_time(args.time)
    days = _parse_days(args.days)

    ac_state: dict[str, object] = {"on": args.state == "on"}
    if args.state == "on":
        if args.mode:
            ac_state["mode"] = args.mode
        if args.target_temperature is not None:
            ac_state["targetTemperature"] = args.target_temperature
        if args.fan_level:
            ac_state["fanLevel"] = args.fan_level

    return {
        "isEnabled": True,
        "acState": ac_state,
        "when": {"time": time_value, "weekDays": days},
    }


def _find_schedule(current: object, schedule_id: str) -> object:
    """Best-effort lookup of ``schedule_id`` in whatever shape ``get_schedules`` returns.

    The response envelope is not documented (``docs/sensibo-api.md`` lists the
    endpoint's path and method only). Handle a bare list, or a dict wrapping
    the list under ``result``; anything else yields ``None`` rather than
    raising — this is purely dry-run context, never load-bearing for the
    delete call itself, which only needs ``pod`` and ``schedule_id``.
    """
    items = current
    if isinstance(items, dict):
        items = items.get("result")
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("id") == schedule_id:
            return item
    return None


def cmd_list(args: argparse.Namespace) -> int:
    client = _client()
    try:
        current = client.get_schedules(args.pod)
    except ApiError as err:
        raise from_api_error(err) from err

    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(read_payload(pod=args.pod, data=current), json_mode=True)
    else:
        emit_result(
            render_read_text("sensibo schedule list", args.pod, current),
            json_mode=False,
        )
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    # Validate/build the requested body BEFORE touching the client: a bad
    # --time/--days/--raw-body is a pure user error and must not trigger real
    # API-key resolution or a network call first.
    requested = _build_schedule_body(args)
    client = _client()
    try:
        current = client.get_schedules(args.pod)
    except ApiError as err:
        raise from_api_error(err) from err

    apply = bool(getattr(args, "apply", False))
    result = None
    if apply:
        try:
            result = client.post_schedules(args.pod, requested)
        except ApiError as err:
            raise from_api_error(err) from err

    payload = build_payload(
        pod=args.pod,
        action="create",
        apply=apply,
        current=current,
        requested=requested,
        result=result,
    )
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(render_write_text("sensibo schedule create", payload), json_mode=False)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    client = _client()
    try:
        schedules = client.get_schedules(args.pod)
    except ApiError as err:
        raise from_api_error(err) from err
    matched = _find_schedule(schedules, args.schedule_id)

    apply = bool(getattr(args, "apply", False))
    result = None
    if apply:
        try:
            result = client.delete_schedule(args.pod, args.schedule_id)
        except ApiError as err:
            raise from_api_error(err) from err

    payload = build_payload(
        pod=args.pod,
        action="delete",
        apply=apply,
        current=matched,
        requested=None,
        result=result,
        schedule_id=args.schedule_id,
    )
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(render_write_text("sensibo schedule delete", payload), json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "schedule",
        help="Recurring server-side automation on a pod (cloud-executed).",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview, json=False)
    noun_sub = p.add_subparsers(dest="schedule_command", parser_class=type(p))

    overview = noun_sub.add_parser("overview", help="Describe the schedule noun.")
    overview.add_argument("--json", action="store_true", help="Emit structured JSON.")
    overview.set_defaults(func=cmd_overview)

    lst = noun_sub.add_parser("list", help="List the schedules configured on a pod.")
    lst.add_argument("pod", help="Sensibo pod id (device id).")
    lst.add_argument("--json", action="store_true", help="Emit structured JSON.")
    lst.set_defaults(func=cmd_list)

    create = noun_sub.add_parser(
        "create", help="Create a schedule (dry-run by default; --apply commits)."
    )
    create.add_argument("pod", help="Sensibo pod id (device id).")
    create.add_argument("--time", help="24-hour HH:MM the schedule fires at.")
    create.add_argument(
        "--days",
        default="all",
        help="Comma-separated weekdays (MON,TUE,...) or 'all' (default: all).",
    )
    create.add_argument(
        "--state",
        choices=["on", "off"],
        default="on",
        help="Whether the schedule turns the AC on or off (default: on).",
    )
    create.add_argument("--mode", help="AC mode to set when --state=on, e.g. cool.")
    create.add_argument(
        "--target-temperature", type=int, help="Target temperature when --state=on."
    )
    create.add_argument("--fan-level", help="Fan level when --state=on, e.g. medium.")
    create.add_argument(
        "--raw-body",
        help="Exact JSON request body, overriding --time/--days/--state/etc.",
    )
    create.add_argument(
        "--apply", action="store_true", help="Commit the change (default: dry-run preview only)."
    )
    create.add_argument("--json", action="store_true", help="Emit structured JSON.")
    create.set_defaults(func=cmd_create)

    delete = noun_sub.add_parser(
        "delete", help="Delete a schedule (dry-run by default; --apply commits)."
    )
    delete.add_argument("pod", help="Sensibo pod id (device id).")
    delete.add_argument("schedule_id", help="The schedule id to delete.")
    delete.add_argument(
        "--apply", action="store_true", help="Commit the change (default: dry-run preview only)."
    )
    delete.add_argument("--json", action="store_true", help="Emit structured JSON.")
    delete.set_defaults(func=cmd_delete)
