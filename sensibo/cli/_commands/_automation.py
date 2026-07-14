"""Shared plumbing for the cloud-automation noun groups (task t8).

``smartmode`` (Climate React), ``schedule``, and ``timer`` all wrap Sensibo's
own SERVER-SIDE automation — the opposite of the local rules engine
(``sensibo/rules/``, a later task). They share one write-verb shape:

* fetch the current server-side state first (powers the dry-run diff);
* build the requested change from CLI flags or a ``--raw-body`` escape hatch
  (Sensibo does not document these endpoints' request-body schemas, only
  their existence — ``docs/sensibo-api.md``'s Endpoints table lists paths and
  methods, nothing about payload shape, so a raw-JSON override lets an
  operator match reality without a code change);
* only call the mutating client method when ``--apply`` is passed;
* render the same {pod, action, apply, current, requested, result} shape in
  both text and ``--json``, always carrying the cloud-execution marker
  (:mod:`sensibo.cli._cloud`).

This module holds no argparse wiring of its own — it is imported by
``smartmode.py``, ``schedule.py``, and ``timer.py``, each of which owns its
own verbs and endpoints.
"""

from __future__ import annotations

import argparse
import json

from sensibo.cli._cloud import EXECUTION_FIELD, execution_marker, execution_text_line
from sensibo.cli._commands.overview import emit_overview
from sensibo.cli._errors import EXIT_USER_ERROR, CliError

# -- shared validation --------------------------------------------------


POD_HELP = "Sensibo pod id (device id)."
JSON_HELP = "Emit structured JSON."


def parse_raw_body(value: str) -> dict[str, object]:
    """Parse ``--raw-body`` into a JSON object, or raise a user-facing CliError."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as err:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"--raw-body is not valid JSON: {err}",
            remediation='pass a JSON object, e.g. --raw-body \'{"acState": {"on": true}}\'',
        ) from err
    if not isinstance(parsed, dict):
        raise CliError(
            code=EXIT_USER_ERROR,
            message="--raw-body must be a JSON object",
            remediation="wrap the body in {...}",
        )
    return parsed


# -- shared result shape --------------------------------------------------


def build_payload(
    *,
    pod: str,
    action: str,
    apply: bool,
    current: object,
    requested: object,
    result: object,
    **extra: object,
) -> dict[str, object]:
    """The {pod, action, apply, current, requested, result, execution} shape."""
    payload: dict[str, object] = {
        "pod": pod,
        "action": action,
        "apply": apply,
        "current": current,
        "requested": requested,
        "result": result,
    }
    payload.update(extra)
    payload.update(execution_marker())
    return payload


def _pretty(value: object) -> str:
    if value is None:
        return "  (none)"
    text = json.dumps(value, indent=2, sort_keys=True)
    return "\n".join(f"  {line}" for line in text.splitlines())


def render_write_text(title: str, payload: dict[str, object]) -> str:
    """Render a write-verb payload (from :func:`build_payload`) as text."""
    lines = [title, execution_text_line(), ""]
    lines.append(f"pod: {payload['pod']}")
    lines.append(f"action: {payload['action']}")
    _rendered_elsewhere = {
        "pod",
        "action",
        "apply",
        "current",
        "requested",
        "result",
        EXECUTION_FIELD,
    }
    for key, value in payload.items():
        if key in _rendered_elsewhere:
            continue
        lines.append(f"{key}: {value}")
    lines.append("current:")
    lines.append(_pretty(payload["current"]))
    lines.append("requested:")
    lines.append(_pretty(payload["requested"]))
    if payload["apply"]:
        lines.append("applied: yes")
        lines.append("result:")
        lines.append(_pretty(payload["result"]))
    else:
        lines.append("applied: no (dry-run — pass --apply to commit)")
    return "\n".join(lines)


def render_read_text(title: str, pod: str, data: object) -> str:
    """Render a read-only verb (show/list) as text."""
    lines = [title, execution_text_line(), "", f"pod: {pod}", "result:", _pretty(data)]
    return "\n".join(lines)


def read_payload(*, pod: str, data: object) -> dict[str, object]:
    payload: dict[str, object] = {"pod": pod, "result": data}
    payload.update(execution_marker())
    return payload


# -- noun overview (architecture.md: "a noun with action-verbs must also
# expose <noun> overview"; see sensibo/cli/_commands/cli.py for the pattern) --


def noun_overview_sections(verb_lines: list[str]) -> list[dict[str, object]]:
    return [
        {"title": "Verbs", "items": list(verb_lines) + ["overview — describe this noun"]},
        {"title": "Execution", "items": [execution_text_line()]},
    ]


def make_overview_command(subject: str, verb_lines: list[str]):
    """Build a ``cmd_overview(args) -> int`` handler for one noun."""

    def cmd_overview(args: argparse.Namespace) -> int:
        emit_overview(
            subject,
            noun_overview_sections(verb_lines),
            json_mode=bool(getattr(args, "json", False)),
        )
        return 0

    return cmd_overview
