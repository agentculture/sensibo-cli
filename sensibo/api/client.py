"""``sensibo.api.client`` — a stdlib HTTP client for the Sensibo cloud API v2.

Zero runtime dependencies: ``urllib.request`` + ``json`` + ``gzip`` + ``io`` +
``time`` + ``os`` + ``random`` only (stdlib). This module does not import
anything from :mod:`sensibo.cli` — :mod:`sensibo.api` is usable as a
standalone library (``docs/architecture.md``, "Where the Sensibo code goes").

Load-bearing facts this client is built around, all recorded in
``docs/sensibo-api.md``:

* the API key travels as the ``apiKey`` **query parameter**, never a header —
  every URL built here is scrubbed before it can reach an exception, a log
  line, or a repr (:mod:`sensibo.api._scrub`);
* ``Accept-Encoding: gzip`` is a rate-limit lever Sensibo documents, not just a
  bandwidth optimisation — every request sends it, and a gzipped response is
  decoded transparently;
* the fleet is polled with **one** call (``GET /users/me/pods?fields=*``),
  never one request per device — :meth:`SensiboClient.fleet_snapshot`;
* ``historicalMeasurements`` is empirically gated to a short window on
  non-Plus accounts and returns HTTP 403 past it —
  :meth:`SensiboClient.get_historical_measurements` raises a typed
  :class:`~sensibo.api._errors.GatedHistoryWindowError`, not a crash.

Every method here is a **thin, uniform** wrapper around exactly one endpoint
from ``docs/sensibo-api.md``'s table — no verb logic, no dry-run/apply logic.
That belongs to the CLI layer (``sensibo/cli/_commands/``), which does not
exist yet for these endpoints.
"""

from __future__ import annotations

import gzip
import io
import json
import random
import re
import time
from collections.abc import Mapping
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sensibo.api._auth import resolve_api_key
from sensibo.api._errors import (
    ERROR_NETWORK,
    ApiError,
    GatedHistoryWindowError,
    HttpError,
    RateLimitExceededError,
)
from sensibo.api._scrub import scrub_url

DEFAULT_BASE_URL = "https://home.sensibo.com/api/v2"
DEFAULT_MIN_INTERVAL = 1.5
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_BACKOFF_JITTER = 0.5
DEFAULT_TIMEOUT = 15.0

_ALL_FIELDS = "*"


def _desensitize_http_error(err: HTTPError) -> None:
    """Scrub the apiKey out of ``err``'s attributes in place.

    ``urllib.error.HTTPError`` stashes the *exact* request URL (raw key and
    all) on ``.url`` and ``.filename`` (inherited from ``OSError``). Mutating
    them here matters even when the caller raises a fresh exception ``from
    None``: Python still sets the fresh exception's ``__context__`` to
    ``err`` — implicit chaining happens regardless of ``from`` — so anyone
    walking the exception chain would otherwise still find the raw key on
    this object.
    """
    for attr in ("url", "filename"):
        value = getattr(err, attr, None)
        if isinstance(value, str):
            setattr(err, attr, scrub_url(value))


def _read_error_body(err: HTTPError) -> str:
    try:
        raw = err.read()
        # Error responses honor Accept-Encoding too — decompress before
        # decoding or the diagnostic is gzip bytes (caught on a real 404).
        if err.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    except Exception:  # noqa: BLE001 - best-effort diagnostic only, never fatal
        return ""
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")[:200]


class SensiboClient:
    """Thin stdlib client for the Sensibo cloud API v2.

    Owns key resolution, gzip, the query-parameter auth quirk, 429 backoff,
    client-side pacing, and URL scrubbing — the concerns ``docs/architecture.md``
    assigns to ``sensibo/api/``. Carries no verb or dry-run logic; every public
    method is a direct wrapper around one endpoint.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_jitter: float = DEFAULT_BACKOFF_JITTER,
        timeout: float = DEFAULT_TIMEOUT,
        env: Mapping[str, str] | None = None,
        home: Path | str | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else resolve_api_key(env=env, home=home)
        self._base_url = base_url.rstrip("/")
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_jitter = backoff_jitter
        self._timeout = timeout
        self._last_request_at: float | None = None

    def __repr__(self) -> str:  # never expose the key via repr/logging
        return f"SensiboClient(base_url={self._base_url!r})"

    # -- client-side pacing --------------------------------------------

    def _throttle(self) -> None:
        if self._min_interval <= 0 or self._last_request_at is None:
            return
        remaining = self._min_interval - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    # -- low-level request -----------------------------------------------

    def _build_url(
        self, path: str, params: dict[str, object] | None, api_version: str | None = None
    ) -> str:
        query: dict[str, object] = dict(params or {})
        query["apiKey"] = self._api_key
        base = self._base_url
        if api_version is not None:
            # timer/ and schedules/ live under v1 in production (CONFIRMED
            # against the real fleet 2026-07-14: the v2 routes are server-level
            # 404s) — the OpenAPI spec's v2 placement is wrong.
            base = re.sub(r"/v\d+$", f"/{api_version}", base)
        return f"{base}{path}?{urlencode(query)}"

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, object] | None = None,
        json_body: dict[str, object] | None = None,
        api_version: str | None = None,
    ) -> object:
        """Issue one HTTP request and return the parsed JSON body (or ``None``).

        Retries on HTTP 429 with exponential backoff plus jitter, bounded by
        ``max_retries``. Every other non-2xx response raises
        :class:`~sensibo.api._errors.HttpError` carrying the HTTP status. A
        client-side minimum interval is enforced once per call, before the
        first attempt.
        """
        url = self._build_url(path, params, api_version)
        data = None
        headers = {"Accept-Encoding": "gzip"}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        attempt = 0
        self._throttle()
        while True:
            req = Request(url, data=data, headers=headers, method=method)
            try:
                # url is always _build_url()'s own https://home.sensibo.com base plus a
                # fixed relative path from this module: never user-controlled input.
                resp = urlopen(req, timeout=self._timeout)  # nosec B310
            except HTTPError as err:
                self._last_request_at = time.monotonic()
                if err.code == 429 and attempt < self._max_retries:
                    # jitter for retry timing only: not a security use of random.
                    delay = self._backoff_base * (2**attempt) + random.uniform(  # nosec B311
                        0, self._backoff_jitter
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue

                body_snippet = _read_error_body(err)
                _desensitize_http_error(err)
                safe_url = scrub_url(url)
                if err.code == 429:
                    raise RateLimitExceededError(
                        message=(
                            f"rate limited (HTTP 429) after {self._max_retries} retries "
                            f"calling {safe_url}: {body_snippet}"
                        ),
                        remediation=(
                            "the client already backs off automatically; poll less often, "
                            "or raise min_interval/max_retries"
                        ),
                    ) from None
                raise HttpError(
                    message=f"HTTP {err.code} calling {safe_url}: {body_snippet}",
                    status=err.code,
                    remediation="check the request parameters and the Sensibo API status",
                ) from None
            except URLError as err:
                self._last_request_at = time.monotonic()
                raise ApiError(
                    code=ERROR_NETWORK,
                    message=f"network error calling {scrub_url(url)}: {err.reason}",
                    remediation="check network connectivity to home.sensibo.com",
                ) from None

            try:
                raw = resp.read()
                encoding = resp.headers.get("Content-Encoding", "")
            finally:
                resp.close()
            self._last_request_at = time.monotonic()

            if encoding.lower() == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))

    # -- pods / fleet ------------------------------------------------------

    def get_pods(self, fields: str = _ALL_FIELDS) -> object:
        """``GET /users/me/pods`` — every pod owned by this account, in ONE call."""
        return self.request("GET", "/users/me/pods", params={"fields": fields})

    def fleet_snapshot(self, fields: str = _ALL_FIELDS) -> object:
        """The one-call fleet poll: ``GET /users/me/pods?fields=*``.

        Callers must never loop :meth:`get_pod` per device for a fleet-wide
        snapshot — this single call embeds every pod's current measurements
        (``docs/sensibo-api.md``, "Poll with one call, not one per device").
        """
        return self.get_pods(fields=fields)

    def get_pod(self, pod_id: str, fields: str | None = None) -> object:
        """``GET /pods/{id}``"""
        params = {"fields": fields} if fields else None
        return self.request("GET", f"/pods/{pod_id}", params=params)

    def get_measurements(self, pod_id: str) -> object:
        """``GET /pods/{id}/measurements`` (undocumented but present in Sensibo's own SDK)."""
        return self.request("GET", f"/pods/{pod_id}/measurements")

    def get_historical_measurements(self, pod_id: str, days: int = 1) -> object:
        """``GET /pods/{id}/historicalMeasurements?days=...``

        HTTP 403 here is an empirically observed gated-window signal
        (``docs/sensibo-api.md``, "History retention"): it is raised as a
        typed :class:`~sensibo.api._errors.GatedHistoryWindowError` instead of
        surfacing as a generic :class:`~sensibo.api._errors.HttpError`, so a
        caller can catch it specifically and step ``days`` down.
        """
        try:
            return self.request(
                "GET",
                f"/pods/{pod_id}/historicalMeasurements",
                params={"days": days},
            )
        except HttpError as err:
            if err.status == 403:
                raise GatedHistoryWindowError(
                    pod_id=pod_id,
                    days=days,
                    message=(
                        f"historicalMeasurements is gated on this account for days={days} "
                        "(HTTP 403)"
                    ),
                    remediation=(
                        "retry with a smaller `days` value — this account's observed "
                        "accessible window is 1 day; a paid tier may raise it"
                    ),
                ) from None
            raise

    # -- acStates: the control surface --------------------------------------

    def get_ac_states(self, pod_id: str, limit: int | None = None) -> object:
        """``GET /pods/{id}/acStates`` (``limit`` maxes out at 20 per Sensibo's docs)."""
        params = {"limit": limit} if limit is not None else None
        return self.request("GET", f"/pods/{pod_id}/acStates", params=params)

    def post_ac_states(self, pod_id: str, ac_state: dict[str, object]) -> object:
        """``POST /pods/{id}/acStates`` — body ``{"acState": {...}}``."""
        return self.request("POST", f"/pods/{pod_id}/acStates", json_body={"acState": ac_state})

    def patch_ac_state(
        self,
        pod_id: str,
        prop: str,
        current_ac_state: dict[str, object],
        new_value: object,
    ) -> object:
        """``PATCH /pods/{id}/acStates/{prop}`` — the safe single-property toggle."""
        return self.request(
            "PATCH",
            f"/pods/{pod_id}/acStates/{prop}",
            json_body={"currentAcState": current_ac_state, "newValue": new_value},
        )

    # -- smartmode: Climate React -------------------------------------------

    def get_smartmode(self, pod_id: str) -> object:
        """``GET /pods/{id}/smartmode``"""
        return self.request("GET", f"/pods/{pod_id}/smartmode")

    def put_smartmode(self, pod_id: str, body: dict[str, object]) -> object:
        """``PUT /pods/{id}/smartmode``"""
        return self.request("PUT", f"/pods/{pod_id}/smartmode", json_body=body)

    def post_smartmode(self, pod_id: str, body: dict[str, object]) -> object:
        """``POST /pods/{id}/smartmode``"""
        return self.request("POST", f"/pods/{pod_id}/smartmode", json_body=body)

    # -- timer (note: trailing slash) ---------------------------------------

    def get_timer(self, pod_id: str) -> object:
        """``GET /pods/{id}/timer/``"""
        return self.request("GET", f"/pods/{pod_id}/timer/", api_version="v1")

    def put_timer(self, pod_id: str, body: dict[str, object]) -> object:
        """``PUT /pods/{id}/timer/``"""
        return self.request("PUT", f"/pods/{pod_id}/timer/", json_body=body, api_version="v1")

    def delete_timer(self, pod_id: str) -> object:
        """``DELETE /pods/{id}/timer/``"""
        return self.request("DELETE", f"/pods/{pod_id}/timer/", api_version="v1")

    # -- schedules (note: trailing slash) ------------------------------------

    def get_schedules(self, pod_id: str) -> object:
        """``GET /pods/{id}/schedules/``"""
        return self.request("GET", f"/pods/{pod_id}/schedules/", api_version="v1")

    def post_schedules(self, pod_id: str, body: dict[str, object]) -> object:
        """``POST /pods/{id}/schedules/``"""
        return self.request("POST", f"/pods/{pod_id}/schedules/", json_body=body, api_version="v1")

    def delete_schedule(self, pod_id: str, schedule_id: str) -> object:
        """``DELETE /pods/{id}/schedules/{schedule_id}/``

        Per-schedule op, trailing slash (``docs/sensibo-api.md``, Endpoints
        table: "Per-schedule ops at ``/schedules/{schedule_id}/``").
        """
        return self.request("DELETE", f"/pods/{pod_id}/schedules/{schedule_id}/", api_version="v1")

    # -- events ----------------------------------------------------------------

    def get_events(self, pod_id: str, limit: int | None = None) -> object:
        """``GET /pods/{id}/events``"""
        params = {"limit": limit} if limit is not None else None
        return self.request("GET", f"/pods/{pod_id}/events", params=params)
