"""``sensibo timer`` — a one-shot server-side countdown on a pod.

Timers run **inside Sensibo's cloud** (``docs/sensibo-api.md``:
``/pods/{id}/timer/`` — note the trailing slash): once set, the countdown
fires even while this operator's machine is asleep. Every verb here carries
the cloud-execution marker (:mod:`sensibo.cli._cloud`).

Verbs:

* ``timer show <pod>`` — read-only, ``GET /pods/{id}/timer/``.
* ``timer set <pod> --minutes N --state on|off`` — write. Dry-run by
  default: shows the current timer and the requested one, and calls
  nothing. ``--apply`` commits via ``PUT /pods/{id}/timer/``.
* ``timer clear <pod>`` — write. Dry-run by default: shows the current
  timer and calls nothing. ``--apply`` commits via
  ``DELETE /pods/{id}/timer/``.

Sensibo documents the ``timer`` endpoint's path and methods but not its
request-body schema. ``set`` builds a ``{"minutesFromNow", "acState"}`` body
from friendly flags; ``--raw-body`` is an escape hatch to send an exact JSON
body when the friendly flags don't match what a real pod expects.
"""

from __future__ import annotations

import argparse

from sensibo.api import ApiError, HttpError, SensiboClient
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
    "timer show <pod> — read the current timer state",
    "timer set <pod> --minutes N --state on|off — set a countdown timer "
    "(dry-run by default; --apply commits)",
    "timer clear <pod> — clear the countdown timer (dry-run by default; --apply commits)",
]
cmd_overview = make_overview_command("sensibo timer", _VERB_LINES)


def _client() -> SensiboClient:
    return SensiboClient()


def _build_timer_body(args: argparse.Namespace) -> dict[str, object]:
    if getattr(args, "raw_body", None):
        return parse_raw_body(args.raw_body)

    if args.minutes is None or args.state is None:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="--minutes and --state are required unless --raw-body is given",
            remediation="pass --minutes N --state on|off",
        )
    if args.minutes <= 0:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"--minutes must be positive, got {args.minutes}",
            remediation="pass a positive integer number of minutes",
        )

    ac_state: dict[str, object] = {"on": args.state == "on"}
    if args.state == "on":
        if args.mode:
            ac_state["mode"] = args.mode
        if args.target_temperature is not None:
            ac_state["targetTemperature"] = args.target_temperature
        if args.fan_level:
            ac_state["fanLevel"] = args.fan_level

    return {"minutesFromNow": args.minutes, "acState": ac_state}


def cmd_show(args: argparse.Namespace) -> int:
    client = _client()
    try:
        current = client.get_timer(args.pod)
    except HttpError as err:
        # A pod with no timer set answers an application-level 404 ("This pod
        # does not have a timer" — confirmed against the real fleet). That is
        # a normal state, not an error.
        if err.status == 404:
            current = {"status": "success", "result": None, "note": "no timer set"}
        else:
            raise from_api_error(err) from err
    except ApiError as err:
        raise from_api_error(err) from err

    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(read_payload(pod=args.pod, data=current), json_mode=True)
    else:
        emit_result(
            render_read_text("sensibo timer show", args.pod, current),
            json_mode=False,
        )
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    # Validate/build the requested body BEFORE touching the client: a bad
    # --minutes/--state/--raw-body is a pure user error and must not trigger
    # real API-key resolution or a network call first.
    requested = _build_timer_body(args)
    client = _client()
    try:
        current = client.get_timer(args.pod)
    except ApiError as err:
        raise from_api_error(err) from err

    apply = bool(getattr(args, "apply", False))
    result = None
    if apply:
        try:
            result = client.put_timer(args.pod, requested)
        except ApiError as err:
            raise from_api_error(err) from err

    payload = build_payload(
        pod=args.pod,
        action="set",
        apply=apply,
        current=current,
        requested=requested,
        result=result,
    )
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(render_write_text("sensibo timer set", payload), json_mode=False)
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    client = _client()
    try:
        current = client.get_timer(args.pod)
    except ApiError as err:
        raise from_api_error(err) from err

    apply = bool(getattr(args, "apply", False))
    result = None
    if apply:
        try:
            result = client.delete_timer(args.pod)
        except ApiError as err:
            raise from_api_error(err) from err

    payload = build_payload(
        pod=args.pod,
        action="clear",
        apply=apply,
        current=current,
        requested=None,
        result=result,
    )
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(render_write_text("sensibo timer clear", payload), json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "timer",
        help="A one-shot server-side countdown on a pod (cloud-executed).",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview, json=False)
    noun_sub = p.add_subparsers(dest="timer_command", parser_class=type(p))

    overview = noun_sub.add_parser("overview", help="Describe the timer noun.")
    overview.add_argument("--json", action="store_true", help="Emit structured JSON.")
    overview.set_defaults(func=cmd_overview)

    show = noun_sub.add_parser("show", help="Read the current timer state.")
    show.add_argument("pod", help="Sensibo pod id (device id).")
    show.add_argument("--json", action="store_true", help="Emit structured JSON.")
    show.set_defaults(func=cmd_show)

    set_ = noun_sub.add_parser(
        "set", help="Set a countdown timer (dry-run by default; --apply commits)."
    )
    set_.add_argument("pod", help="Sensibo pod id (device id).")
    set_.add_argument("--minutes", type=int, help="Minutes from now the timer fires.")
    set_.add_argument("--state", choices=["on", "off"], help="AC state the timer sets.")
    set_.add_argument("--mode", help="AC mode to set when --state=on, e.g. cool.")
    set_.add_argument("--target-temperature", type=int, help="Target temperature when --state=on.")
    set_.add_argument("--fan-level", help="Fan level when --state=on, e.g. medium.")
    set_.add_argument(
        "--raw-body",
        help="Exact JSON request body, overriding --minutes/--state/etc.",
    )
    set_.add_argument(
        "--apply", action="store_true", help="Commit the change (default: dry-run preview only)."
    )
    set_.add_argument("--json", action="store_true", help="Emit structured JSON.")
    set_.set_defaults(func=cmd_set)

    clear = noun_sub.add_parser(
        "clear", help="Clear the countdown timer (dry-run by default; --apply commits)."
    )
    clear.add_argument("pod", help="Sensibo pod id (device id).")
    clear.add_argument(
        "--apply", action="store_true", help="Commit the change (default: dry-run preview only)."
    )
    clear.add_argument("--json", action="store_true", help="Emit structured JSON.")
    clear.set_defaults(func=cmd_clear)
