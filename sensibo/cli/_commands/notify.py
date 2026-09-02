"""``sensibo notify`` — send (or preview) a test notification (task t6).

Wraps :mod:`sensibo.notify` so an operator can verify their configured
transport(s) — ``SENSIBO_NOTIFY_WEBHOOK`` / ``SENSIBO_NOTIFY_SCRIPT`` — work,
without waiting for a real sensor-down event.

**Dry-run by default**, same convention as every other write verb in this
project: without ``--apply``, ``notify test`` resolves the configured
transports and prints the exact redacted payload
(:func:`sensibo.notify.render_dry_run`) it would send, and calls
:func:`sensibo.notify.send` **zero times**. With ``--apply`` it calls
:func:`sensibo.notify.send` **exactly once**.

With no transport configured: the dry-run preview says so and exits 0 (there
is nothing broken, just nothing configured); ``--apply`` with nothing
configured is a user error (exit 1) naming both env vars in its remediation,
since sending would be a silent no-op otherwise.

Every response — text and ``--json`` — carries the local-execution marker
(:data:`sensibo.health.EXECUTION_LOCAL`): notifications only fire while this
machine's collector is running, unlike Sensibo's own cloud automation.
"""

from __future__ import annotations

import argparse
import time

import sensibo.notify as notify_pkg
from sensibo.cli._commands._automation import JSON_HELP
from sensibo.cli._commands.overview import emit_overview
from sensibo.cli._errors import EXIT_USER_ERROR, CliError
from sensibo.cli._output import emit_result
from sensibo.health import EXECUTION_LOCAL, iso8601
from sensibo.notify import (
    NotifyConfig,
    Outcome,
    Payload,
    redact,
    render_dry_run,
    resolve_notify_config,
)

#: The field name every local-execution payload carries (matches
#: sensibo/rules/model.py's EXECUTION_FIELD and sensibo/cli/_cloud.py's own).
EXECUTION_FIELD = "execution"

_NOT_CONFIGURED_REMEDIATION = (
    "set SENSIBO_NOTIFY_WEBHOOK and/or SENSIBO_NOTIFY_SCRIPT "
    "(environment or ~/.sensibo/.env) to configure a notify transport"
)


def _test_payload() -> Payload:
    ts = iso8601(time.time())
    return Payload(
        kind="test",
        location="(none)",
        status="test",
        since=ts,
        last_ok=ts,
        message="test notification from `sensibo notify test`",
    )


def _configured_transports(config: NotifyConfig) -> list[dict[str, str]]:
    transports: list[dict[str, str]] = []
    if config.webhook_url:
        transports.append({"transport": "webhook", "target": redact(config.webhook_url, config)})
    if config.script_path:
        transports.append({"transport": "script", "target": config.script_path})
    return transports


def _payload_dict(payload: Payload, config: NotifyConfig) -> dict[str, object]:
    return {
        "kind": payload.kind,
        "location": payload.location,
        "status": payload.status,
        "since": payload.since,
        "last_ok": payload.last_ok,
        "message": redact(payload.message, config),
        EXECUTION_FIELD: payload.execution,
    }


def _outcomes_to_list(outcomes: list[Outcome]) -> list[dict[str, object]]:
    return [{"transport": o.transport, "ok": o.ok, "detail": o.detail} for o in outcomes]


def cmd_notify_test(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    apply = bool(getattr(args, "apply", False))
    config = resolve_notify_config()
    payload = _test_payload()

    if not apply:
        if json_mode:
            emit_result(
                {
                    "apply": False,
                    "sent": False,
                    "payload": _payload_dict(payload, config),
                    "transports": _configured_transports(config),
                    "outcomes": [],
                    EXECUTION_FIELD: EXECUTION_LOCAL,
                },
                json_mode=True,
            )
        else:
            lines = [render_dry_run(payload, config)]
            if not config.configured:
                lines.append("not configured: no transport is set (nothing would be sent)")
            lines.append(f"{EXECUTION_FIELD}: {EXECUTION_LOCAL}")
            emit_result("\n".join(lines), json_mode=False)
        return 0

    if not config.configured:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="no notify transport configured",
            remediation=_NOT_CONFIGURED_REMEDIATION,
        )

    outcomes = notify_pkg.send(payload, config)
    if json_mode:
        emit_result(
            {
                "apply": True,
                "sent": True,
                "payload": _payload_dict(payload, config),
                "transports": _configured_transports(config),
                "outcomes": _outcomes_to_list(outcomes),
                EXECUTION_FIELD: EXECUTION_LOCAL,
            },
            json_mode=True,
        )
    else:
        lines = [f"sent test notification to {len(outcomes)} transport(s):", ""]
        for outcome in outcomes:
            mark = "ok" if outcome.ok else "FAILED"
            lines.append(f"  {outcome.transport}: {mark} ({outcome.detail})")
        lines.append(f"{EXECUTION_FIELD}: {EXECUTION_LOCAL}")
        emit_result("\n".join(lines), json_mode=False)
    return 0


def _notify_sections() -> list[dict[str, object]]:
    return [
        {
            "title": "Verbs",
            "items": [
                "test [--apply] — preview (default) or send a test notification",
                "overview — describe this noun",
            ],
        },
        {"title": "Execution", "items": [f"{EXECUTION_FIELD}: {EXECUTION_LOCAL}"]},
    ]


def cmd_notify_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "sensibo notify",
        _notify_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    # `sensibo notify` with no sub-verb prints the noun's overview.
    return cmd_notify_overview(args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "notify",
        help="Notification transport: send a test alert (see 'sensibo notify overview').",
    )
    p.add_argument("--json", action="store_true", help=JSON_HELP)
    p.set_defaults(func=_no_verb)
    # Propagate the structured-error parser class so every sub-verb's parse
    # errors route through the CliError contract, not argparse's default.
    notify_sub = p.add_subparsers(dest="notify_command", parser_class=type(p))

    ov = notify_sub.add_parser("overview", help="Describe the notify noun.")
    ov.add_argument("--json", action="store_true", help=JSON_HELP)
    ov.set_defaults(func=cmd_notify_overview)

    test_p = notify_sub.add_parser(
        "test",
        help="Preview (default) or send a test notification through configured transports.",
    )
    test_p.add_argument(
        "--apply",
        action="store_true",
        help="Actually send the test notification (default: dry-run preview only).",
    )
    test_p.add_argument("--json", action="store_true", help=JSON_HELP)
    test_p.set_defaults(func=cmd_notify_test)
