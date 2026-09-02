"""The LAN dashboard's HTTP server: stdlib ``http.server``, reads open, writes token-gated.

Task t12's core deliverable. Built on :class:`http.server.ThreadingHTTPServer`
so one slow browser (or a stalled control POST waiting on the Sensibo cloud)
never blocks a concurrent page load — zero runtime dependencies, per
``docs/architecture.md``.

**Routing summary**

======================  ======  ====================================================
Path                    Method  What
======================  ======  ====================================================
``/``                   GET     Dashboard: every location, latest readings, staleness
``/location/<id>``      GET     One location: latest readings, history sparklines,
                                 and (pods only) the control form
``/api/locations``      GET     JSON: every location (mirrors ``sensibo query locations``)
``/api/latest``         GET     JSON: latest reading(s) (``?location=&field=``)
``/api/history``        GET     JSON: a field's time series, bounded (``?location=&field=``
                                 ``[&since=][&until=][&limit=]``, default limit 1000)
``/reports/<name>``     GET     One offline ``*.svg`` report, from the reports directory
                                 (task t9; ``?SENSIBO_REPORTS_DIR`` or ``~/.sensibo/reports/``)
``/control``            POST    HTML control result (dry-run, or applied with ``confirm``)
``/api/set``            POST    JSON control result, same token/confirm contract
======================  ======  ====================================================

**Every GET handler reads only from the local sqlite store** — never the
Sensibo cloud. That is what lets the dashboard work with the cloud
unreachable (``docs/web.md``, "Offline property"). Only the two POST
endpoints construct a :class:`~sensibo.api.SensiboClient` (via
``client_factory``, injectable for tests), and only to preview/apply the one
pod's requested change.

**Control reuses ``sensibo set``'s own dry-run/apply function.**
:func:`sensibo.cli._commands.set._process_pod` is imported directly rather
than reimplemented, so the web dashboard's control form and ``sensibo set``
share one diff/patch/post code path and can never silently drift apart on
what counts as "a write". Same for ``_FLAG_TO_FIELD``, the flag-name ->
``acState``-field mapping.

**Auth.** Write endpoints require the token configured on this
:class:`WebServer` (see :mod:`sensibo.web._token`), supplied either as the
``token`` form field or the ``X-Sensibo-Token`` header, checked with
:func:`sensibo.web._token.check_token` (constant-time). GET endpoints never
check it — the recorded operator decision is reads open, writes gated.

**Bounded reads (Qodo review 3581287838).** Before this fix,
``/location/<id>`` fetched a field's *entire* time series
(``Store.query_range`` with no bound) and rendered one SVG point per
reading — after months of ~90s-cadence collection a single page load could
trace a polyline with tens of thousands of points. Now:

* The location page requests a bounded lookback window
  (:data:`_HISTORY_WINDOW_SECONDS`, default 24h) capped at
  :data:`_HISTORY_FETCH_LIMIT` rows per field — both applied SQL-side via
  ``Store.query_range``'s ``limit`` (an inner ``ORDER BY timestamp DESC
  LIMIT ?``, never a Python fetchall-then-slice).
* :func:`sensibo.web._svg.render_sparkline` additionally downsamples evenly
  to at most :data:`sensibo.web._svg.DEFAULT_MAX_POINTS` points per chart.
* ``/api/history`` takes explicit ``since``/``until``/``limit`` query
  params, defaulting to :data:`_DEFAULT_API_HISTORY_LIMIT` readings and
  capped at :data:`_MAX_API_HISTORY_LIMIT` regardless of what a caller asks
  for. The applied ``limit`` is echoed back in the JSON payload.

**Bounded POST bodies (Qodo review 3581287840).** This server binds
``0.0.0.0`` by default (:data:`DEFAULT_BIND_HOST`), and ``Content-Length`` is
read *before* the write token is ever checked — so, before this fix, any LAN
client (not just an authenticated one) could force unbounded memory use by
claiming an enormous ``Content-Length``. ``do_POST`` now validates the
header before reading a single byte: missing -> ``411``, non-integer or
negative -> ``400``, larger than :data:`MAX_POST_BYTES` -> ``413`` (returned
without ever calling ``rfile.read``).
"""

from __future__ import annotations

import json as _json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from sensibo.api import ApiError, SensiboClient
from sensibo.cli._commands.set import _FLAG_TO_FIELD, _process_pod
from sensibo.cli._errors import CliError
from sensibo.cli._output import emit_diagnostic
from sensibo.health.model import HealthConfig
from sensibo.store import Store

from . import _render
from ._token import check_token
from ._wire import location_to_dict, reading_to_dict

#: Default bind address: LAN-reachable, not loopback-only — the recorded
#: operator decision (docs/specs/..., "Web dashboard access") is that reads
#: are open to the network by default; `--bind 127.0.0.1:PORT` opts out.
#: See docs/web.md, "Trust model".
DEFAULT_BIND_HOST = "0.0.0.0"  # nosec B104
DEFAULT_BIND_PORT = 8323

#: ``sensibo web``'s reports directory env override (task t9). Mirrors the
#: ``~/.sensibo``-rooted convention every other path in this project uses
#: (``sensibo/store/_paths.py``, ``sensibo/web/_token.py``).
ENV_REPORTS_DIR = "SENSIBO_REPORTS_DIR"

#: Default location for the offline SVG reports this server exposes read-only
#: under ``/reports/<name>`` (task t9). Same trust model as the rest of the
#: dashboard's GET surface: open reads, no token.
DEFAULT_REPORTS_DIR = Path.home() / ".sensibo" / "reports"

_CONTROL_FIELDS = ("power", "mode", "target", "fan", "swing")
_TRUTHY = {"1", "true", "yes", "on"}

#: Default lookback window for the location page's history sparklines
#: (Qodo 3581287838) — recent history, not the store's entire retention
#: window (which defaults to two years, per ``sensibo.store.store``).
_HISTORY_WINDOW_SECONDS = 24 * 60 * 60  # 24h

#: Belt-and-braces SQL-side cap on rows fetched per field for the location
#: page, on top of the window above — protects a single page load even if a
#: pod's collection cadence is far tighter than the ~90s norm.
_HISTORY_FETCH_LIMIT = 2000

#: `/api/history`'s default `?limit=` when the caller doesn't pass one.
_DEFAULT_API_HISTORY_LIMIT = 1000

#: Hard ceiling on `/api/history`'s `?limit=`, regardless of what a caller
#: asks for — same rationale as `_HISTORY_FETCH_LIMIT`.
_MAX_API_HISTORY_LIMIT = 10_000

#: Hard cap on a POST body's size (Qodo 3581287840). This server binds
#: `0.0.0.0` by default (`DEFAULT_BIND_HOST`), and `Content-Length` is
#: validated before the write token is ever checked, so any LAN client —
#: authenticated or not — must never be able to force unbounded memory use
#: by claiming an enormous `Content-Length`.
MAX_POST_BYTES = 64 * 1024


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUTHY


def _requested_from_form(form: dict[str, str]) -> dict[str, Any]:
    """Translate web form fields into an ``acState`` change dict.

    Deliberately the same flag -> field mapping ``sensibo set`` uses
    (:data:`sensibo.cli._commands.set._FLAG_TO_FIELD`) so a request built
    here diffs identically to one built from CLI flags.
    """
    changes: dict[str, Any] = {}
    power = form.get("power")
    if power:
        changes[_FLAG_TO_FIELD["power"]] = power == "on"
    mode = form.get("mode")
    if mode:
        changes[_FLAG_TO_FIELD["mode"]] = mode
    target = form.get("target")
    if target not in (None, ""):
        changes[_FLAG_TO_FIELD["target"]] = int(target)
    fan = form.get("fan")
    if fan:
        changes[_FLAG_TO_FIELD["fan"]] = fan
    swing = form.get("swing")
    if swing:
        changes[_FLAG_TO_FIELD["swing"]] = swing
    return changes


def _parse_body(raw: bytes, content_type: str) -> dict[str, str]:
    """Parse a POST body as JSON or ``application/x-www-form-urlencoded``.

    The HTML control form posts form-encoded; ``/api/set`` accepts either, so
    a script can just ``json.dumps`` a body without hand-rolling
    form-encoding. Values are coerced to ``str`` (except booleans, kept as-is
    for ``confirm``) — every field this module reads is parsed from strings
    anyway (``_requested_from_form``, ``_truthy``).
    """
    text = raw.decode("utf-8") if raw else ""
    if not text:
        return {}
    if "application/json" in content_type:
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): v if isinstance(v, bool) else str(v) for k, v in data.items()}
    parsed = parse_qs(text, keep_blank_values=True)
    return {k: v[-1] for k, v in parsed.items()}


def _resolve_reports_dir(reports_dir: "str | os.PathLike[str] | None") -> Path:
    """Resolve the reports directory (task t9): explicit arg, then
    ``SENSIBO_REPORTS_DIR``, then :data:`DEFAULT_REPORTS_DIR`.

    Never touches the filesystem — a missing directory resolves to a path
    like any other; :func:`_list_reports` is what turns "missing" into an
    empty listing rather than an error.
    """
    if reports_dir is not None:
        return Path(reports_dir)
    override = os.environ.get(ENV_REPORTS_DIR)
    if override:
        return Path(override)
    return DEFAULT_REPORTS_DIR


def _list_reports(reports_dir: Path) -> list[str]:
    """Every ``*.svg`` report's filename, newest first. Empty if the
    directory doesn't exist yet — not an error (task t9 criterion 3)."""
    if not reports_dir.is_dir():
        return []
    entries = [p for p in reports_dir.iterdir() if p.is_file() and p.suffix == ".svg"]
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in entries]


def _safe_report_path(reports_dir: Path, name: str) -> Path | None:
    """The report file ``name`` resolves to, or ``None`` if ``name`` is
    unsafe (path traversal) or not an ``.svg`` (task t9 criterion 3).

    Rejects on the raw (already-unquoted) name rather than trusting
    ``Path.resolve()`` to sort it out — no ``/``, no ``..``, no leading dot
    segment, and the file must actually end in ``.svg``.
    """
    if not name or not name.endswith(".svg"):
        return None
    if "/" in name or "\\" in name or ".." in name:
        return None
    return reports_dir / name


class _NotFound(Exception):
    pass


class _Handler(BaseHTTPRequestHandler):
    server_version = "sensibo-web/1"
    server: "WebServer"  # narrows the type of self.server for the methods below

    # -- logging: route to stderr diagnostics, never stdout --------------

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        emit_diagnostic(f"web: {self.address_string()} - {format % args}")

    # -- GET ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == "/":
                self._get_index()
            elif path.startswith("/location/"):
                self._get_location(unquote(path[len("/location/") :]))
            elif path == "/api/locations":
                self._get_api_locations()
            elif path == "/api/latest":
                self._get_api_latest(query)
            elif path == "/api/history":
                self._get_api_history(query)
            elif path.startswith("/reports/"):
                self._get_report(unquote(path[len("/reports/") :]))
            else:
                self._send_text(404, "not found\n")
        except _NotFound as err:
            self._send_text(404, f"{err}\n")
        except Exception as err:  # noqa: BLE001 - never crash the handler thread
            self._send_text(500, f"internal error: {err}\n")

    def _get_index(self) -> None:
        with Store(db_path=self.server.db_path) as store:
            rows = [
                (loc, store.latest_readings(loc.id), store.get_health(loc.id))
                for loc in store.list_locations()
            ]
            last_cycle_at = store.get_meta("last_cycle_at")
            last_cycle_outcome = store.get_meta("last_cycle_outcome")
        reports = _list_reports(self.server.reports_dir)
        body = _render.render_index(
            rows,
            stale_after_hours=self.server.stale_after_hours,
            last_cycle_at=last_cycle_at,
            last_cycle_outcome=last_cycle_outcome,
            reports=reports,
        )
        self._send_html(200, body)

    def _get_location(self, location_id: str) -> None:
        with Store(db_path=self.server.db_path) as store:
            loc = store.get_location(location_id)
            if loc is None:
                raise _NotFound(f"unknown location: {location_id}")
            latest = store.latest_readings(loc.id)
            health = store.get_health(loc.id)
            # Bounded lookback window + SQL-side row cap (Qodo 3581287838):
            # never fetch a field's entire history to render one page.
            since = time.time() - _HISTORY_WINDOW_SECONDS
            history = {
                field: store.query_range(loc.id, field, since=since, limit=_HISTORY_FETCH_LIMIT)
                for field in latest
            }
        body = _render.render_location(
            loc, latest, history, stale_after_hours=self.server.stale_after_hours, health=health
        )
        self._send_html(200, body)

    def _get_api_locations(self) -> None:
        with Store(db_path=self.server.db_path) as store:
            locations = store.list_locations()
            health_by_id = {h.location_id: h for h in store.list_health()}
        payload = {
            "locations": [
                location_to_dict(
                    loc,
                    stale_after_hours=self.server.stale_after_hours,
                    health=health_by_id.get(loc.id),
                )
                for loc in locations
            ]
        }
        self._send_json(200, payload)

    def _get_report(self, name: str) -> None:
        """Serve one ``*.svg`` from the reports directory (task t9,
        criterion 3). Same trust model as every other GET: open, no token.
        """
        path = _safe_report_path(self.server.reports_dir, name)
        if path is None or not path.is_file():
            raise _NotFound(f"unknown report: {name}")
        self._send_svg(200, path.read_bytes())

    def _get_api_latest(self, query: dict[str, str]) -> None:
        location_id = query.get("location")
        field = query.get("field")
        with Store(db_path=self.server.db_path) as store:
            if location_id is not None:
                loc = store.get_location(location_id)
                targets = [loc] if loc is not None else []
            else:
                targets = store.list_locations()

            readings = []
            for loc in targets:
                if field:
                    reading = store.latest_reading(loc.id, field)
                    if reading is not None:
                        readings.append(reading)
                else:
                    readings.extend(store.latest_readings(loc.id).values())

        readings.sort(key=lambda r: (r.location_id, r.field))
        self._send_json(200, {"readings": [reading_to_dict(r) for r in readings]})

    def _get_api_history(self, query: dict[str, str]) -> None:
        location_id = query.get("location")
        field = query.get("field")
        if not location_id or not field:
            self._send_text(400, "both ?location= and ?field= are required\n")
            return
        try:
            since = float(query["since"]) if query.get("since") else None
            until = float(query["until"]) if query.get("until") else None
        except ValueError:
            self._send_text(400, "?since= and ?until= must be numeric epoch seconds\n")
            return

        # Bounded by default (Qodo 3581287838): a caller gets at most
        # `_DEFAULT_API_HISTORY_LIMIT` readings unless it explicitly asks
        # for a different `?limit=`, itself capped at `_MAX_API_HISTORY_LIMIT`.
        limit = _DEFAULT_API_HISTORY_LIMIT
        if query.get("limit"):
            try:
                limit = int(query["limit"])
            except ValueError:
                self._send_text(400, "?limit= must be an integer\n")
                return
            if limit <= 0:
                self._send_text(400, "?limit= must be a positive integer\n")
                return
            limit = min(limit, _MAX_API_HISTORY_LIMIT)

        with Store(db_path=self.server.db_path) as store:
            loc = store.get_location(location_id)
            if loc is None:
                raise _NotFound(f"unknown location: {location_id}")
            readings = store.query_range(loc.id, field, since=since, until=until, limit=limit)

        self._send_json(
            200,
            {
                "location_id": location_id,
                "field": field,
                "limit": limit,
                "readings": [reading_to_dict(r) for r in readings],
            },
        )

    # -- POST (writes: token-gated) -----------------------------------------

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        path = parsed.path

        # Content-Length must be validated *before* a single byte is read
        # (Qodo 3581287840): this server binds 0.0.0.0 by default, so an
        # unauthenticated LAN client must never be able to force unbounded
        # memory use through this header alone.
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._send_text(411, "Content-Length is required\n")
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._send_text(400, "Content-Length must be an integer\n")
            return
        if length < 0:
            self._send_text(400, "Content-Length must not be negative\n")
            return
        if length > MAX_POST_BYTES:
            self._send_text(413, f"request body exceeds the {MAX_POST_BYTES}-byte limit\n")
            return

        raw = self.rfile.read(length) if length else b""
        content_type = self.headers.get("Content-Type", "")
        form = _parse_body(raw, content_type)
        try:
            if path == "/control":
                self._post_control(form, json_mode=False)
            elif path == "/api/set":
                self._post_control(form, json_mode=True)
            else:
                self._send_text(404, "not found\n")
        except Exception as err:  # noqa: BLE001 - never crash the handler thread
            self._send_text(500, f"internal error: {err}\n")

    def _extract_token(self, form: dict[str, str]) -> str | None:
        header_token = self.headers.get("X-Sensibo-Token")
        if header_token:
            return header_token
        token = form.get("token")
        return str(token) if token is not None else None

    def _post_control(self, form: dict[str, Any], *, json_mode: bool) -> None:
        candidate = self._extract_token(form)
        if not check_token(candidate, self.server.token):
            self._respond_control_error(401, "missing or invalid token", json_mode=json_mode)
            return

        pod_id = form.get("pod_id")
        if not pod_id:
            self._respond_control_error(400, "pod_id is required", json_mode=json_mode)
            return

        requested = _requested_from_form(
            {k: str(v) for k, v in form.items() if k in _CONTROL_FIELDS}
        )
        if not requested:
            self._respond_control_error(
                400,
                "no fields given to change (power/mode/target/fan/swing)",
                json_mode=json_mode,
            )
            return

        confirm = _truthy(form.get("confirm"))
        client = self.server.client_factory()
        try:
            entry = _process_pod(client, str(pod_id), requested, apply=confirm)
        except ApiError as err:
            self._respond_control_error(502, err.message, json_mode=json_mode)
            return
        except CliError as err:
            self._respond_control_error(400, err.message, json_mode=json_mode)
            return

        payload: dict[str, Any] = {"applied": confirm, **entry}
        if json_mode:
            self._send_json(200, payload)
        else:
            form_values = {k: str(v) for k, v in form.items() if k not in ("token",)}
            self._send_html(200, _render.render_control_result(payload, form=form_values))

    def _respond_control_error(self, status: int, message: str, *, json_mode: bool) -> None:
        if json_mode:
            self._send_json(status, {"error": message})
        else:
            self._send_html(status, _render.render_error(message, status=status))

    # -- response helpers -----------------------------------------------------

    def _send_html(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, payload: object) -> None:
        data = _json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, status: int, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_svg(self, status: int, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class WebServer(ThreadingHTTPServer):
    """The dashboard server: one thread per request, so a slow control POST
    (waiting on the real Sensibo cloud) never blocks a concurrent page load.

    ``client_factory`` defaults to the real :class:`SensiboClient` (built
    fresh per write request, matching how every CLI verb builds one per
    invocation); tests inject a fake to prove writes never touch the network.

    ``stale_after_hours`` defaults to ``None``, which resolves at construction
    time — the CLI/server boundary — via ``HealthConfig.from_env().
    down_after_seconds / 3600.0`` (task t9): the single source of truth for
    staleness, honoring an operator's ``SENSIBO_HEALTH_DOWN_AFTER`` override.
    Tests inject an explicit value for determinism, same as before.

    ``reports_dir`` resolves the same way (task t9, criterion 3): an explicit
    argument wins, then ``SENSIBO_REPORTS_DIR``, then
    ``~/.sensibo/reports/`` (:data:`DEFAULT_REPORTS_DIR`).
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        db_path: str | None = None,
        token: str,
        client_factory: Callable[[], object] = SensiboClient,
        stale_after_hours: float | None = None,
        reports_dir: "str | os.PathLike[str] | None" = None,
    ) -> None:
        super().__init__(server_address, _Handler)
        self.db_path = db_path
        self.token = token
        self.client_factory = client_factory
        self.stale_after_hours = (
            stale_after_hours
            if stale_after_hours is not None
            else HealthConfig.from_env().down_after_seconds / 3600.0
        )
        self.reports_dir = _resolve_reports_dir(reports_dir)
