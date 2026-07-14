"""API key resolution: ``SENSIBO_API_KEY`` env var, then ``~/.sensibo/.env``.

The order is load-bearing (see ``docs/sensibo-api.md`` and ``CLAUDE.md``,
"Secrets") and covered by ``tests/test_api_auth.py``. This module does not
import anything from :mod:`sensibo.cli` — see :mod:`sensibo.api._errors`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from sensibo.api._errors import MissingApiKeyError

ENV_VAR = "SENSIBO_API_KEY"
API_KEY_PORTAL_URL = "https://home.sensibo.com/me/api"
_DOTENV_RELATIVE_PATH = Path(".sensibo") / ".env"


def _dotenv_path(home: Path | str | None) -> Path:
    if home is not None:
        base = Path(home)
    else:
        base = Path(os.environ.get("HOME") or Path.home())
    return base / _DOTENV_RELATIVE_PATH


def _parse_dotenv(text: str) -> dict[str, str]:
    """Parse simple ``KEY=VALUE`` lines: no interpolation, no export keyword.

    Blank lines and lines starting with ``#`` are skipped. Values may be
    wrapped in matching single or double quotes, which are stripped.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def resolve_api_key(
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> str:
    """Resolve the Sensibo API key: ``SENSIBO_API_KEY`` env var first, then dotenv.

    ``env`` and ``home`` are injectable so callers (and tests) never have to
    touch the real process environment or the real ``~/.sensibo/.env`` file.
    Production code should call this with no arguments, which reads
    ``os.environ`` and the real ``$HOME``.

    Raises :class:`~sensibo.api._errors.MissingApiKeyError` if neither source
    has a non-empty value.
    """
    environ = env if env is not None else os.environ

    from_env = environ.get(ENV_VAR)
    if from_env:
        return from_env

    dotenv_path = _dotenv_path(home)
    if dotenv_path.is_file():
        values = _parse_dotenv(dotenv_path.read_text(encoding="utf-8"))
        from_file = values.get(ENV_VAR)
        if from_file:
            return from_file

    raise MissingApiKeyError(
        message=(
            f"no Sensibo API key found ({ENV_VAR} is unset and {dotenv_path} "
            f"has no {ENV_VAR} line)"
        ),
        remediation=(
            f"mint a key at {API_KEY_PORTAL_URL} and either "
            f"export {ENV_VAR}=<key>, or write '{ENV_VAR}=<key>' to {dotenv_path}"
        ),
    )
