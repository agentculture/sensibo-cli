"""``sensibo smartmode`` — Climate React, Sensibo's own SERVER-SIDE automation.

Climate React runs **inside Sensibo's cloud**: once enabled it keeps enforcing
its thresholds even if this operator's machine (and any local rules engine
built in a later task) is asleep or offline. Every verb here says so via
:mod:`sensibo.cli._cloud` — that is an acceptance criterion, not decoration.

Verbs:

* ``smartmode show <pod>`` — read-only, ``GET /pods/{id}/smartmode``.
* ``smartmode enable <pod>`` / ``disable <pod>`` — writes. **Dry-run by
  default**: prints the current config, the requested ``{"enabled": ...}``
  change, and the diff, and calls nothing. ``--apply`` commits via
  ``PUT /pods/{id}/smartmode``.

Sensibo documents the ``smartmode`` endpoint's existence and methods
(``docs/sensibo-api.md``) but not its request-body schema, so the enable/
disable body here is the minimal, uncontroversial ``{"enabled": bool}`` —
not a full Climate React threshold configuration (no ``configure`` verb
exists yet).
"""

from __future__ import annotations

import argparse

from sensibo.api import ApiError, SensiboClient
from sensibo.cli._apierrors import from_api_error
from sensibo.cli._commands._automation import (
    build_payload,
    make_overview_command,
    read_payload,
    render_read_text,
    render_write_text,
)
from sensibo.cli._output import emit_result

_VERB_LINES = [
    "smartmode show <pod> — read the current Climate React config",
    "smartmode enable <pod> — turn Climate React on (dry-run by default; --apply commits)",
    "smartmode disable <pod> — turn Climate React off (dry-run by default; --apply commits)",
]
cmd_overview = make_overview_command("sensibo smartmode", _VERB_LINES)


def _client() -> SensiboClient:
    return SensiboClient()


def cmd_show(args: argparse.Namespace) -> int:
    client = _client()
    try:
        current = client.get_smartmode(args.pod)
    except ApiError as err:
        raise from_api_error(err) from err

    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(read_payload(pod=args.pod, data=current), json_mode=True)
    else:
        emit_result(
            render_read_text("sensibo smartmode show", args.pod, current),
            json_mode=False,
        )
    return 0


def _enable_disable(args: argparse.Namespace, *, enabled: bool, action: str) -> int:
    client = _client()
    try:
        current = client.get_smartmode(args.pod)
    except ApiError as err:
        raise from_api_error(err) from err

    requested = {"enabled": enabled}
    apply = bool(getattr(args, "apply", False))
    result = None
    if apply:
        try:
            result = client.put_smartmode(args.pod, requested)
        except ApiError as err:
            raise from_api_error(err) from err

    payload = build_payload(
        pod=args.pod,
        action=action,
        apply=apply,
        current=current,
        requested=requested,
        result=result,
    )
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(render_write_text(f"sensibo smartmode {action}", payload), json_mode=False)
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    return _enable_disable(args, enabled=True, action="enable")


def cmd_disable(args: argparse.Namespace) -> int:
    return _enable_disable(args, enabled=False, action="disable")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "smartmode",
        help="Climate React — Sensibo's server-side threshold automation (cloud-executed).",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview, json=False)
    noun_sub = p.add_subparsers(dest="smartmode_command", parser_class=type(p))

    overview = noun_sub.add_parser("overview", help="Describe the smartmode noun.")
    overview.add_argument("--json", action="store_true", help="Emit structured JSON.")
    overview.set_defaults(func=cmd_overview)

    show = noun_sub.add_parser("show", help="Read the current Climate React config.")
    show.add_argument("pod", help="Sensibo pod id (device id).")
    show.add_argument("--json", action="store_true", help="Emit structured JSON.")
    show.set_defaults(func=cmd_show)

    enable = noun_sub.add_parser(
        "enable", help="Turn Climate React on (dry-run by default; --apply commits)."
    )
    enable.add_argument("pod", help="Sensibo pod id (device id).")
    enable.add_argument(
        "--apply", action="store_true", help="Commit the change (default: dry-run preview only)."
    )
    enable.add_argument("--json", action="store_true", help="Emit structured JSON.")
    enable.set_defaults(func=cmd_enable)

    disable = noun_sub.add_parser(
        "disable", help="Turn Climate React off (dry-run by default; --apply commits)."
    )
    disable.add_argument("pod", help="Sensibo pod id (device id).")
    disable.add_argument(
        "--apply", action="store_true", help="Commit the change (default: dry-run preview only)."
    )
    disable.add_argument("--json", action="store_true", help="Emit structured JSON.")
    disable.set_defaults(func=cmd_disable)
