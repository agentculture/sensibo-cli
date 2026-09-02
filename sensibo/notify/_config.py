"""Notification transport config: ``SENSIBO_NOTIFY_WEBHOOK`` / ``SENSIBO_NOTIFY_SCRIPT``.

Resolution mirrors :mod:`sensibo.api._auth` (see ``CLAUDE.md``, "Secrets"): the
environment first, then ``~/.sensibo/.env`` — the operator-maintained canonical
file (chmod 600) — parsed with the same simple ``KEY=VALUE`` scanner. The
webhook URL is a secret (a Discord webhook URL grants post rights), so callers
must run it through :func:`sensibo.notify.transport.redact` before it reaches a
log line, a ``--json`` payload, or a dry-run preview.

This module does not import anything from :mod:`sensibo.cli`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sensibo.api._auth import _dotenv_path, _parse_dotenv

WEBHOOK_VAR = "SENSIBO_NOTIFY_WEBHOOK"
SCRIPT_VAR = "SENSIBO_NOTIFY_SCRIPT"

#: Default per-transport timeout in seconds (webhook POST and script hook alike).
DEFAULT_TIMEOUT = 10.0


@dataclass(frozen=True)
class NotifyConfig:
    """The resolved notification transports; either may be absent."""

    webhook_url: str | None = None
    script_path: str | None = None
    timeout: float = DEFAULT_TIMEOUT

    @property
    def configured(self) -> bool:
        """True if at least one transport is set."""
        return self.webhook_url is not None or self.script_path is not None


def resolve_notify_config(
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> NotifyConfig:
    """Resolve notify config: environment first, then ``~/.sensibo/.env``.

    ``env`` and ``home`` are injectable so callers (and tests) never have to
    touch the real process environment or the real ``~/.sensibo/.env`` file.
    Production code should call this with no arguments, which reads
    ``os.environ`` and the real ``$HOME``. An empty value counts as unset.
    """
    environ = env if env is not None else os.environ

    dotenv_values: dict[str, str] = {}
    dotenv_path = _dotenv_path(home)
    if dotenv_path.is_file():
        dotenv_values = _parse_dotenv(dotenv_path.read_text(encoding="utf-8"))

    def _resolve(var: str) -> str | None:
        from_env = environ.get(var)
        if from_env:
            return from_env
        from_file = dotenv_values.get(var)
        return from_file or None

    return NotifyConfig(
        webhook_url=_resolve(WEBHOOK_VAR),
        script_path=_resolve(SCRIPT_VAR),
    )
