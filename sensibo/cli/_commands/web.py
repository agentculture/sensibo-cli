"""``sensibo web`` — the LAN dashboard: reads open, writes token-gated (task t12).

Serves ``sensibo.web.WebServer`` (stdlib ``http.server``): live readings per
location, staleness flags, and inline SVG history charts, all rendered from
the local sqlite store — the dashboard works with the cloud unreachable. A
control form per pod mirrors ``sensibo set``'s dry-run/apply contract.

Binds ``0.0.0.0:8323`` by default — **LAN-reachable, not loopback-only**. That
is the recorded operator decision (``docs/specs/...``, "Web dashboard
access"): reads are open to anyone who can reach this host; only writes
require the token this command generates (once) and persists to
``~/.sensibo/web-token`` (mode 600, override with ``--token-file``). The path
is printed to stderr; the token value never is.

Testing seam: :func:`_serve` wraps the blocking ``server.serve_forever()``
call so a test can monkeypatch it to a no-op and inspect the constructed
:class:`~sensibo.web.WebServer` instead of hanging.
"""

from __future__ import annotations

import argparse

from sensibo.cli._errors import EXIT_USER_ERROR, CliError
from sensibo.cli._output import emit_diagnostic, emit_result
from sensibo.web import (
    DEFAULT_BIND_HOST,
    DEFAULT_BIND_PORT,
    WebServer,
    default_token_path,
    ensure_token,
)


def _parse_bind(value: str) -> tuple[str, int]:
    """Parse ``--bind``'s ``ADDR:PORT`` shape.

    ``rpartition`` (not ``split``) so an IPv6 literal's own colons don't
    confuse the split — only the last colon separates host from port.
    """
    host, sep, port_text = value.rpartition(":")
    if not sep:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid --bind value: {value!r} (expected ADDR:PORT)",
            remediation="pass --bind as ADDR:PORT, e.g. --bind 0.0.0.0:8323",
        )
    if not host:
        host = DEFAULT_BIND_HOST
    try:
        port = int(port_text)
    except ValueError as err:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid port in --bind: {port_text!r}",
            remediation="pass --bind as ADDR:PORT with a numeric port, e.g. 0.0.0.0:8323",
        ) from err
    return host, port


def _serve(server: WebServer) -> None:
    """Block serving requests. A seam tests replace to avoid actually blocking."""
    server.serve_forever()


def cmd_web(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    host, port = _parse_bind(args.bind) if args.bind else (DEFAULT_BIND_HOST, DEFAULT_BIND_PORT)

    token_path = args.token_file if args.token_file else default_token_path()
    token, token_path = ensure_token(token_path)

    server = WebServer((host, port), db_path=args.db, token=token)
    bound_host, bound_port = server.server_address[:2]

    emit_diagnostic(f"sensibo web: token file: {token_path}")
    emit_diagnostic(
        "sensibo web: reads are OPEN to anyone who can reach this host; "
        f"writes require the token in {token_path}"
    )
    emit_diagnostic(f"sensibo web: serving on http://{bound_host}:{bound_port}/ (Ctrl-C to stop)")

    summary = {
        "bind": f"{bound_host}:{bound_port}",
        "db": str(args.db) if args.db else None,
        "token_file": str(token_path),
    }
    emit_result(
        summary if json_mode else f"sensibo web: listening on {bound_host}:{bound_port}",
        json_mode=json_mode,
    )

    try:
        _serve(server)
    except KeyboardInterrupt:
        emit_diagnostic("sensibo web: stopped")
    finally:
        server.server_close()
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "web",
        help="Serve the LAN dashboard: reads open, writes token-gated (stdlib http.server).",
    )
    p.add_argument(
        "--bind",
        default=None,
        metavar="ADDR:PORT",
        help=f"Bind address (default: {DEFAULT_BIND_HOST}:{DEFAULT_BIND_PORT}, LAN-reachable).",
    )
    p.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Path to the local sqlite store (default: $SENSIBO_DB or ~/.sensibo/sensibo.db).",
    )
    p.add_argument(
        "--token-file",
        default=None,
        metavar="PATH",
        help=f"Where the write-auth token is read/persisted (default: {default_token_path()}).",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_web)
