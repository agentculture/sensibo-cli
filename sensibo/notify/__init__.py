"""``sensibo.notify`` — notification transport: webhook POST and script hook.

The leaf that gets an alert out of the machine once the alerter has decided
one is due (task t3). A :class:`Payload` carries the same local-execution
marker every rule output does (:data:`sensibo.health.EXECUTION_LOCAL`), so
a receiver always knows alerts stop when this daemon stops.

Layering rule (mirrors ``sensibo.rules``): this package imports only stdlib,
plus :mod:`sensibo.api._auth` (dotenv parsing) and :mod:`sensibo.health`
(the execution marker); it must never import :mod:`sensibo.cli`. Delivery
failures are data, not exceptions — :func:`send` returns one :class:`Outcome`
per configured transport and never raises.

Quick example::

    from sensibo.notify import Payload, resolve_notify_config, send

    config = resolve_notify_config()  # env first, then ~/.sensibo/.env
    outcomes = send(
        Payload(kind="sensor_down", location="ms_o7dH4GeY", status="down",
                since="2026-09-02T05:52", last_ok="2026-09-02T05:51",
                message="spare-room sensor down"),
        config,
    )
"""

from __future__ import annotations

from sensibo.notify._config import (
    DEFAULT_TIMEOUT,
    SCRIPT_VAR,
    WEBHOOK_VAR,
    NotifyConfig,
    resolve_notify_config,
)
from sensibo.notify.transport import (
    REDACTED_WEBHOOK,
    TRANSPORT_NONE,
    TRANSPORT_SCRIPT,
    TRANSPORT_WEBHOOK,
    Outcome,
    Payload,
    redact,
    render_dry_run,
    send,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "NotifyConfig",
    "Outcome",
    "Payload",
    "REDACTED_WEBHOOK",
    "SCRIPT_VAR",
    "TRANSPORT_NONE",
    "TRANSPORT_SCRIPT",
    "TRANSPORT_WEBHOOK",
    "redact",
    "render_dry_run",
    "resolve_notify_config",
    "send",
    "WEBHOOK_VAR",
]
