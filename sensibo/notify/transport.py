"""Notification transport: a generic webhook POST and an operator script hook.

A :class:`Payload` is the JSON document every notification carries — the same
shape the alerter builds (``kind``, ``location``, ``status``, ``since``,
``last_ok``, ``message``) plus the local-execution marker, so any consumer that
receives it knows alerts stop when this daemon stops. :func:`send` delivers to
every configured transport and returns one :class:`Outcome` per transport; it
never raises — delivery failures are *data* (a failed outcome with a redacted
detail), because a flaky webhook must not take down the collector cycle that
detected the outage.

The webhook URL is a secret (it grants post rights), so anything that might be
logged — an error detail, a dry-run preview — goes through :func:`redact`. The
script hook runs with ``shell=False`` on a fixed argv list and a child
environment stripped of ``SENSIBO_API_KEY`` and ``SENSIBO_NOTIFY_WEBHOOK``, so
an operator script cannot echo the API key or the webhook back into a log.

This module does not import anything from :mod:`sensibo.cli`.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from collections.abc import Collection
from dataclasses import asdict, dataclass

from sensibo import __version__
from sensibo.health import EXECUTION_LOCAL
from sensibo.notify._config import NotifyConfig

#: Transport names used in :class:`Outcome.transport`.
#: Sent on every webhook POST. Cloudflare-fronted receivers (Discord among
#: them) reject urllib's default ``Python-urllib/x.y`` agent with
#: 403 "error code: 1010"; a named agent is accepted (verified 2026-09-02).
USER_AGENT = f"sensibo-cli/{__version__}"

TRANSPORT_WEBHOOK = "webhook"
TRANSPORT_SCRIPT = "script"
TRANSPORT_NONE = "none"

#: What :func:`redact` replaces the webhook URL with.
REDACTED_WEBHOOK = "https://…(redacted)"

#: Child-env keys the script hook never sees (secrets, per spec h24).
_STRIP_FROM_CHILD_ENV = ("SENSIBO_API_KEY", "SENSIBO_NOTIFY_WEBHOOK")


@dataclass(frozen=True)
class Payload:
    """One outbound notification, ready for JSON serialisation."""

    kind: str
    location: str
    status: str
    since: str
    last_ok: str
    message: str
    execution: str = EXECUTION_LOCAL

    def to_json(self) -> str:
        """Compact JSON — the exact body sent over the wire / piped to stdin.

        Besides the structured fields, the human ``message`` is mirrored into
        ``content`` (what a Discord webhook renders) and ``text`` (Slack,
        Mattermost, and most generic receivers), so a plain webhook URL from
        those services shows the alert without any service-specific client.
        Verified against Discord on 2026-09-02: without ``content`` it answers
        400 "Cannot send an empty message".
        """
        body = asdict(self)
        body["content"] = self.message
        body["text"] = self.message
        return json.dumps(body, separators=(",", ":"))


@dataclass(frozen=True)
class Outcome:
    """The result of delivering one payload to one transport."""

    transport: str
    ok: bool
    detail: str


def redact(text: str, config: NotifyConfig) -> str:
    """Replace any occurrence of the configured webhook URL with ``REDACTED_WEBHOOK``.

    Safe with no webhook configured (returned unchanged).
    """
    if config.webhook_url:
        text = text.replace(config.webhook_url, REDACTED_WEBHOOK)
    return text


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every 3xx: a redirect would re-POST the payload to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _open(request: urllib.request.Request, timeout: float):
    """The one seam through which the webhook POST leaves the process."""
    return _OPENER.open(request, timeout=timeout)


def _send_webhook(payload: Payload, config: NotifyConfig) -> Outcome:
    url = config.webhook_url or ""
    try:
        request = urllib.request.Request(
            url,
            data=payload.to_json().encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        # The URL is operator-configured (an https:// webhook), so scheme auditing
        # is a non-issue here; same pattern as sensibo/api/client.py.
        with _open(request, timeout=config.timeout):  # nosec B310
            pass
    except urllib.error.HTTPError as err:
        return Outcome(TRANSPORT_WEBHOOK, False, redact(f"HTTP {err.code}", config))
    except urllib.error.URLError as err:
        return Outcome(TRANSPORT_WEBHOOK, False, redact(f"network error: {err.reason}", config))
    except ValueError as err:
        # e.g. an unparseable/unknown-scheme webhook URL: Request() itself raises
        # before any network call is made. A setup error, not a delivery error,
        # but send() never raises so it becomes a failed Outcome the same way.
        return Outcome(TRANSPORT_WEBHOOK, False, redact(f"invalid webhook URL: {err}", config))
    return Outcome(TRANSPORT_WEBHOOK, True, "delivered")


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _STRIP_FROM_CHILD_ENV:
        env.pop(key, None)
    return env


def _send_script(payload: Payload, config: NotifyConfig) -> Outcome:
    script_path = config.script_path or ""
    try:
        proc = (
            subprocess.run(  # nosec B603 - fixed argv list, shell=False, no user string splitting
                [script_path],
                input=payload.to_json(),
                shell=False,
                timeout=config.timeout,
                env=_child_env(),
                capture_output=True,
                text=True,
            )
        )
    except subprocess.TimeoutExpired:
        return Outcome(
            TRANSPORT_SCRIPT, False, redact(f"timed out after {config.timeout}s", config)
        )
    except OSError as err:
        # A missing or non-executable script (FileNotFoundError, PermissionError,
        # …) never even spawns; a setup error, not a delivery error, but send()
        # never raises so it becomes a failed Outcome the same way.
        return Outcome(TRANSPORT_SCRIPT, False, redact(f"cannot run script: {err}", config))
    if proc.returncode != 0:
        return Outcome(TRANSPORT_SCRIPT, False, redact(f"exit code {proc.returncode}", config))
    return Outcome(TRANSPORT_SCRIPT, True, "delivered")


def send(
    payload: Payload,
    config: NotifyConfig,
    only: Collection[str] | None = None,
) -> list[Outcome]:
    """Deliver ``payload`` to every configured transport; one :class:`Outcome` each.

    ``only``, when given, restricts delivery to the named transports (e.g.
    ``["script"]`` to retry just the leg that failed last time, without
    re-delivering to the one that already succeeded).

    With neither transport configured, returns
    ``[Outcome('none', False, 'not configured')]``. Never raises: webhook HTTP
    errors and network failures, script non-zero exits and timeouts all become
    failed outcomes whose ``detail`` is already redacted.
    """
    if not config.configured:
        return [Outcome(TRANSPORT_NONE, False, "not configured")]

    outcomes: list[Outcome] = []
    if config.webhook_url and (only is None or TRANSPORT_WEBHOOK in only):
        try:
            outcomes.append(_send_webhook(payload, config))
        except Exception as err:  # noqa: BLE001 - send() never raises by contract;
            # any error the helper itself didn't already turn into an Outcome
            # (an unanticipated bug, not a documented failure mode) still must
            # not escape the collector cycle that calls this.
            outcomes.append(
                Outcome(TRANSPORT_WEBHOOK, False, redact(f"unexpected error: {err}", config))
            )
    if config.script_path and (only is None or TRANSPORT_SCRIPT in only):
        try:
            outcomes.append(_send_script(payload, config))
        except Exception as err:  # noqa: BLE001 - send() never raises; see above.
            outcomes.append(
                Outcome(TRANSPORT_SCRIPT, False, redact(f"unexpected error: {err}", config))
            )
    if not outcomes:
        return [Outcome(TRANSPORT_NONE, False, "no matching transport configured")]
    return outcomes


def render_dry_run(payload: Payload, config: NotifyConfig) -> str:
    """Render what :func:`send` would do, without doing it.

    Multi-line preview naming each configured transport (the webhook URL
    redacted) and carrying the exact message text. Performs no network call
    and spawns no subprocess — the ``sensibo notify test`` verb prints this by
    default and only calls :func:`send` with ``--apply``.
    """
    lines = ["would notify (dry run; nothing was sent):"]
    if config.webhook_url:
        lines.append(f"  webhook: POST {redact(config.webhook_url, config)}")
    if config.script_path:
        lines.append(f"  script:  {config.script_path} (JSON payload on stdin)")
    if not config.configured:
        lines.append("  (no transport configured)")
    lines.append(f"  message: {payload.message}")
    lines.append(f"  execution: {payload.execution}")
    return "\n".join(lines)
