"""``sensibo.web`` — the LAN dashboard: stdlib ``http.server``, reads open, writes token-gated.

Task t12. Pages and JSON endpoints render **entirely from the local sqlite
store** (:mod:`sensibo.store`) — the dashboard needs zero cloud access to
browse live readings, staleness, and history. Only the control form's writes
construct a :class:`~sensibo.api.SensiboClient` and reach Sensibo's cloud, and
only through :func:`sensibo.cli._commands.set._process_pod` — the exact same
dry-run/apply function ``sensibo set`` uses.

Recorded operator decision (``docs/specs/...``, "Web dashboard access"):
**reads are open on the LAN; writes are token-gated.** See ``docs/web.md`` for
the full trust model, quickstart, and the offline property.

Zero runtime dependencies: ``http.server``, ``hmac``, ``secrets``, ``html``,
``json``, and ``urllib.parse`` only (stdlib).

Public surface
--------------

* :class:`WebServer` — a :class:`http.server.ThreadingHTTPServer` subclass;
  construct one, then call ``.serve_forever()``.
* :data:`DEFAULT_BIND_HOST`, :data:`DEFAULT_BIND_PORT` — ``0.0.0.0:8323``,
  LAN-reachable by default (a deliberate choice, not an oversight — see
  ``docs/web.md``, "Trust model").
* :func:`ensure_token`, :func:`check_token`, :func:`default_token_path` — the
  write-auth token's lifecycle (:mod:`sensibo.web._token`).
"""

from __future__ import annotations

from ._token import DEFAULT_TOKEN_FILE, check_token, default_token_path, ensure_token
from .server import DEFAULT_BIND_HOST, DEFAULT_BIND_PORT, WebServer

__all__ = [
    "WebServer",
    "DEFAULT_BIND_HOST",
    "DEFAULT_BIND_PORT",
    "DEFAULT_TOKEN_FILE",
    "default_token_path",
    "ensure_token",
    "check_token",
]
