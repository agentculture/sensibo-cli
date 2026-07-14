"""Tests for sensibo.api.client.SensiboClient.

Hard rule: never make a real network call. Every test mocks
``sensibo.api.client.urlopen`` - the one seam the client calls through - with a
fake that records the built ``urllib.request.Request`` and returns a canned
response (or raises a canned ``urllib.error.HTTPError``).
"""

from __future__ import annotations

import gzip
import io
import json
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlsplit

import pytest

import sensibo.api.client as client_module
from sensibo.api._errors import (
    ApiError,
    GatedHistoryWindowError,
    HttpError,
    RateLimitExceededError,
)
from sensibo.api.client import SensiboClient

API_KEY = "TESTKEY"


# --- fakes -------------------------------------------------------------


class _FakeHeaders:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self._data = {k.lower(): v for k, v in (data or {}).items()}

    def get(self, name: str, default: str = "") -> str:
        return self._data.get(name.lower(), default)


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = _FakeHeaders(headers)
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


class _SingleFakeUrlopen:
    """Always returns the same canned response; records every request made."""

    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list = []

    def __call__(self, req, timeout=None):  # noqa: ANN001 - test double, matches urlopen sig
        self.calls.append(req)
        return self.response


class _SequenceFakeUrlopen:
    """Returns/raises each item from ``items`` in order; records every request made."""

    def __init__(self, items: list) -> None:
        self._items = list(items)
        self.calls: list = []

    def __call__(self, req, timeout=None):  # noqa: ANN001 - test double, matches urlopen sig
        self.calls.append(req)
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _make_client(monkeypatch: pytest.MonkeyPatch, fake_urlopen) -> SensiboClient:
    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    return SensiboClient(api_key=API_KEY, min_interval=0, max_retries=0)


def _split(url: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(url)
    return parts.path, dict(parse_qsl(parts.query))


def _json_response(payload: object) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload).encode("utf-8"))


# --- gzip + auth query param ---------------------------------------------


def test_every_request_sends_accept_encoding_gzip_header(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": []}))
    client = _make_client(monkeypatch, fake)
    client.get_pods()
    assert fake.calls[0].headers.get("Accept-encoding") == "gzip"


def test_gzip_encoded_response_is_transparently_decoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"result": [{"id": "pod1"}]}
    compressed = gzip.compress(json.dumps(payload).encode("utf-8"))
    fake = _SingleFakeUrlopen(_FakeResponse(compressed, headers={"Content-Encoding": "gzip"}))
    client = _make_client(monkeypatch, fake)
    assert client.get_pods() == payload


def test_plain_response_without_gzip_header_still_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"result": []}
    fake = _SingleFakeUrlopen(_json_response(payload))
    client = _make_client(monkeypatch, fake)
    assert client.get_pods() == payload


def test_empty_response_body_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_FakeResponse(b""))
    client = _make_client(monkeypatch, fake)
    assert client.delete_timer("pod1") is None


def test_api_key_sent_as_apikey_query_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": []}))
    client = _make_client(monkeypatch, fake)
    client.get_pods()
    _, query = _split(fake.calls[0].full_url)
    assert query["apiKey"] == API_KEY


def test_client_repr_never_includes_api_key() -> None:
    client = SensiboClient(api_key="TOTALLY-SECRET-VALUE", min_interval=0)
    assert "TOTALLY-SECRET-VALUE" not in repr(client)


# --- 429 backoff -----------------------------------------------------------


def test_429_retries_with_exponential_backoff_and_jitter_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SensiboClient(
        api_key=API_KEY, min_interval=0, max_retries=5, backoff_base=1.0, backoff_jitter=0.5
    )
    items = [
        HTTPError("https://x/?apiKey=K", 429, "Too Many Requests", {}, io.BytesIO(b"")),
        HTTPError("https://x/?apiKey=K", 429, "Too Many Requests", {}, io.BytesIO(b"")),
        _json_response({"ok": True}),
    ]
    fake = _SequenceFakeUrlopen(items)
    monkeypatch.setattr(client_module, "urlopen", fake)

    sleeps: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(client_module.random, "uniform", lambda a, b: 0.25)

    result = client.get_pod("abc")

    assert result == {"ok": True}
    assert len(fake.calls) == 3
    # backoff_base * 2**attempt + jitter, attempt = 0 then 1
    assert sleeps == [pytest.approx(1.25), pytest.approx(2.25)]


def test_429_retry_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SensiboClient(
        api_key=API_KEY, min_interval=0, max_retries=2, backoff_base=0.01, backoff_jitter=0.0
    )
    always_429 = [
        HTTPError("https://x/?apiKey=K", 429, "Too Many Requests", {}, io.BytesIO(b"slow"))
        for _ in range(10)
    ]
    fake = _SequenceFakeUrlopen(always_429)
    monkeypatch.setattr(client_module, "urlopen", fake)
    monkeypatch.setattr(client_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(client_module.random, "uniform", lambda a, b: 0.0)

    with pytest.raises(RateLimitExceededError):
        client.get_pod("abc")

    # initial attempt + max_retries retries, never unbounded
    assert len(fake.calls) == 3


def test_429_retry_honors_retry_after_header_in_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SensiboClient(
        api_key=API_KEY, min_interval=0, max_retries=1, backoff_base=0.01, backoff_jitter=0.0
    )
    items = [
        HTTPError(
            "https://x/?apiKey=K",
            429,
            "Too Many Requests",
            {"Retry-After": "7"},
            io.BytesIO(b""),
        ),
        _json_response({"ok": True}),
    ]
    fake = _SequenceFakeUrlopen(items)
    monkeypatch.setattr(client_module, "urlopen", fake)

    sleeps: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(client_module.random, "uniform", lambda a, b: 0.0)

    result = client.get_pod("abc")

    assert result == {"ok": True}
    assert len(sleeps) == 1
    assert sleeps[0] >= 7


def test_429_retry_after_unparseable_falls_back_to_computed_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SensiboClient(
        api_key=API_KEY, min_interval=0, max_retries=1, backoff_base=1.0, backoff_jitter=0.0
    )
    items = [
        HTTPError(
            "https://x/?apiKey=K",
            429,
            "Too Many Requests",
            {"Retry-After": "not-a-number-or-a-date"},
            io.BytesIO(b""),
        ),
        _json_response({"ok": True}),
    ]
    fake = _SequenceFakeUrlopen(items)
    monkeypatch.setattr(client_module, "urlopen", fake)

    sleeps: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(client_module.random, "uniform", lambda a, b: 0.0)

    result = client.get_pod("abc")

    assert result == {"ok": True}
    # falls back to the plain exponential backoff (attempt 0): base * 2**0 == 1.0
    assert sleeps == [pytest.approx(1.0)]


def test_429_retry_after_http_date_form_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SensiboClient(
        api_key=API_KEY, min_interval=0, max_retries=1, backoff_base=1.0, backoff_jitter=0.0
    )
    items = [
        HTTPError(
            "https://x/?apiKey=K",
            429,
            "Too Many Requests",
            # a syntactically valid HTTP-date, but long past -> non-positive delta
            {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
            io.BytesIO(b""),
        ),
        _json_response({"ok": True}),
    ]
    fake = _SequenceFakeUrlopen(items)
    monkeypatch.setattr(client_module, "urlopen", fake)

    sleeps: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(client_module.random, "uniform", lambda a, b: 0.0)

    result = client.get_pod("abc")

    assert result == {"ok": True}
    # past date parses to a non-positive delta, so the computed backoff (1.0) wins
    assert sleeps == [pytest.approx(1.0)]


def test_429_retry_after_is_capped_and_retries_stay_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SensiboClient(
        api_key=API_KEY, min_interval=0, max_retries=2, backoff_base=0.01, backoff_jitter=0.0
    )
    always_429 = [
        HTTPError(
            "https://x/?apiKey=K",
            429,
            "Too Many Requests",
            {"Retry-After": "999999"},  # hostile: would hang the client if honored raw
            io.BytesIO(b"slow"),
        )
        for _ in range(10)
    ]
    fake = _SequenceFakeUrlopen(always_429)
    monkeypatch.setattr(client_module, "urlopen", fake)

    sleeps: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(client_module.random, "uniform", lambda a, b: 0.0)

    with pytest.raises(RateLimitExceededError):
        client.get_pod("abc")

    # initial attempt + max_retries retries, never unbounded
    assert len(fake.calls) == 3
    assert sleeps
    assert all(s <= 120 for s in sleeps)


# --- client-side rate limiting ----------------------------------------------


def test_default_min_interval_constant_is_one_point_five_seconds() -> None:
    assert client_module.DEFAULT_MIN_INTERVAL == 1.5


def test_min_interval_zero_disables_throttling(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": []}))
    client = _make_client(monkeypatch, fake)  # min_interval=0 via _make_client
    sleeps: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", lambda s: sleeps.append(s))

    client.get_pods()
    client.get_pods()

    assert sleeps == []


def test_client_side_rate_limiting_sleeps_for_remaining_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SensiboClient(api_key=API_KEY, min_interval=10.0, max_retries=0)
    fake = _SequenceFakeUrlopen([_json_response({"a": 1}), _json_response({"a": 2})])
    monkeypatch.setattr(client_module, "urlopen", fake)

    # monotonic() is called: once at the end of call #1 (sets last-request-at),
    # once at the start of call #2 (throttle, elapsed check), once at the end
    # of call #2 (sets last-request-at again).
    clock = iter([100.0, 103.0, 103.5])
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(clock))

    sleeps: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", lambda s: sleeps.append(s))

    client.get_pod("abc")
    client.get_pod("abc")

    assert sleeps == [pytest.approx(7.0)]  # 10.0 - (103.0 - 100.0)


# --- key never leaks --------------------------------------------------------


def test_api_key_never_appears_in_raised_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = "SUPER-SECRET-VALUE-1234"
    client = SensiboClient(api_key=api_key, min_interval=0, max_retries=0)
    captured: dict[str, str] = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001 - test double, matches urlopen sig
        captured["url"] = req.full_url
        raise HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"forbidden"))

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)

    with pytest.raises(ApiError) as exc_info:
        client.get_pod("abc123")

    # sanity: prove the key really was in the outbound URL, so scrubbing had work to do
    assert api_key in captured["url"]

    chain = []
    node = exc_info.value
    seen_ids: set[int] = set()
    while node is not None and id(node) not in seen_ids:
        seen_ids.add(id(node))
        chain.append(node)
        node = node.__cause__ if node.__cause__ is not None else node.__context__

    assert len(chain) >= 1
    for member in chain:
        assert api_key not in str(member)
        assert api_key not in repr(member)
        for value in vars(member).values():
            assert api_key not in str(value)


# --- fleet snapshot: exactly one HTTP call ----------------------------------


def test_fleet_snapshot_performs_exactly_one_http_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "result": [
            {"id": "pod1", "measurements": {"temperature": 21}},
            {"id": "pod2", "measurements": {"temperature": 19}},
        ]
    }
    fake = _SingleFakeUrlopen(_json_response(payload))
    client = _make_client(monkeypatch, fake)

    result = client.fleet_snapshot()

    assert result == payload
    assert len(fake.calls) == 1
    req = fake.calls[0]
    assert req.get_method() == "GET"
    path, query = _split(req.full_url)
    assert path == "/api/v2/users/me/pods"
    assert query["fields"] == "*"


def test_fleet_snapshot_fields_is_parameterizable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": []}))
    client = _make_client(monkeypatch, fake)

    client.fleet_snapshot(fields="temperature,humidity")

    _, query = _split(fake.calls[0].full_url)
    assert query["fields"] == "temperature,humidity"


def test_get_pods_is_the_fleet_snapshot_primitive(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": []}))
    client = _make_client(monkeypatch, fake)
    client.get_pods()
    assert len(fake.calls) == 1
    path, query = _split(fake.calls[0].full_url)
    assert path == "/api/v2/users/me/pods"
    assert query["fields"] == "*"


# --- thin primitives: one test per endpoint from docs/sensibo-api.md -------


def test_get_pod_details(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"id": "pod1"}))
    client = _make_client(monkeypatch, fake)
    client.get_pod("pod1", fields="temperature,humidity")
    req = fake.calls[0]
    assert req.get_method() == "GET"
    path, query = _split(req.full_url)
    assert path == "/api/v2/pods/pod1"
    assert query["fields"] == "temperature,humidity"


def test_get_measurements(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": []}))
    client = _make_client(monkeypatch, fake)
    client.get_measurements("pod1")
    path, _ = _split(fake.calls[0].full_url)
    assert path == "/api/v2/pods/pod1/measurements"


def test_get_historical_measurements_defaults_to_days_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": []}))
    client = _make_client(monkeypatch, fake)
    client.get_historical_measurements("pod1")
    path, query = _split(fake.calls[0].full_url)
    assert path == "/api/v2/pods/pod1/historicalMeasurements"
    assert query["days"] == "1"


def test_get_historical_measurements_days_is_parameterizable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": []}))
    client = _make_client(monkeypatch, fake)
    client.get_historical_measurements("pod1", days=7)
    _, query = _split(fake.calls[0].full_url)
    assert query["days"] == "7"


def test_get_historical_measurements_403_is_a_gated_window_signal_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(req, timeout=None):  # noqa: ANN001 - test double, matches urlopen sig
        raise HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"gated"))

    client = _make_client(monkeypatch, fake_urlopen)

    with pytest.raises(GatedHistoryWindowError) as exc_info:
        client.get_historical_measurements("pod1", days=90)

    err = exc_info.value
    assert err.pod_id == "pod1"
    assert err.days == 90
    assert err.status == 403
    assert err.remediation


def test_network_error_raises_api_error_with_scrubbed_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.error import URLError

    api_key = "NETWORK-ERROR-SECRET"

    def fake_urlopen(req, timeout=None):  # noqa: ANN001 - test double, matches urlopen sig
        raise URLError("Name or service not known")

    client = SensiboClient(api_key=api_key, min_interval=0, max_retries=0)
    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)

    with pytest.raises(ApiError) as exc_info:
        client.get_pods()

    assert api_key not in str(exc_info.value)
    assert "Name or service not known" in str(exc_info.value)


def test_error_body_read_failure_is_swallowed_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_read_error_body`` must never itself blow up while building an error message."""

    class _UnreadableHTTPError(HTTPError):
        def read(self, *args, **kwargs):  # noqa: ANN001 - matches HTTPError.read signature
            raise OSError("boom")

    def fake_urlopen(req, timeout=None):  # noqa: ANN001 - test double, matches urlopen sig
        raise _UnreadableHTTPError(req.full_url, 500, "Server Error", {}, io.BytesIO(b""))

    client = _make_client(monkeypatch, fake_urlopen)

    with pytest.raises(HttpError) as exc_info:
        client.get_pods()

    assert exc_info.value.status == 500


def test_get_historical_measurements_non_403_error_is_plain_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(req, timeout=None):  # noqa: ANN001 - test double, matches urlopen sig
        raise HTTPError(req.full_url, 500, "Server Error", {}, io.BytesIO(b"oops"))

    client = _make_client(monkeypatch, fake_urlopen)

    with pytest.raises(HttpError) as exc_info:
        client.get_historical_measurements("pod1", days=1)

    assert exc_info.value.status == 500
    assert not isinstance(exc_info.value, GatedHistoryWindowError)


def test_get_ac_states(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": []}))
    client = _make_client(monkeypatch, fake)
    client.get_ac_states("pod1", limit=20)
    req = fake.calls[0]
    assert req.get_method() == "GET"
    path, query = _split(req.full_url)
    assert path == "/api/v2/pods/pod1/acStates"
    assert query["limit"] == "20"


def test_post_ac_states_body_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": {}}))
    client = _make_client(monkeypatch, fake)
    ac_state = {"on": True, "mode": "cool", "targetTemperature": 22}
    client.post_ac_states("pod1", ac_state)
    req = fake.calls[0]
    assert req.get_method() == "POST"
    path, _ = _split(req.full_url)
    assert path == "/api/v2/pods/pod1/acStates"
    assert json.loads(req.data.decode("utf-8")) == {"acState": ac_state}


def test_patch_ac_state_single_property_body_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": {}}))
    client = _make_client(monkeypatch, fake)
    current = {"on": True, "mode": "cool"}
    client.patch_ac_state("pod1", "targetTemperature", current, 24)
    req = fake.calls[0]
    assert req.get_method() == "PATCH"
    path, _ = _split(req.full_url)
    assert path == "/api/v2/pods/pod1/acStates/targetTemperature"
    assert json.loads(req.data.decode("utf-8")) == {
        "currentAcState": current,
        "newValue": 24,
    }


def test_get_smartmode(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": {}}))
    client = _make_client(monkeypatch, fake)
    client.get_smartmode("pod1")
    req = fake.calls[0]
    assert req.get_method() == "GET"
    path, _ = _split(req.full_url)
    assert path == "/api/v2/pods/pod1/smartmode"


def test_put_smartmode(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": {}}))
    client = _make_client(monkeypatch, fake)
    body = {"enabled": True}
    client.put_smartmode("pod1", body)
    req = fake.calls[0]
    assert req.get_method() == "PUT"
    assert json.loads(req.data.decode("utf-8")) == body


def test_post_smartmode(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": {}}))
    client = _make_client(monkeypatch, fake)
    body = {"type": "temperature"}
    client.post_smartmode("pod1", body)
    req = fake.calls[0]
    assert req.get_method() == "POST"
    assert json.loads(req.data.decode("utf-8")) == body


def test_timer_endpoints_use_a_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": {}}))
    client = _make_client(monkeypatch, fake)

    client.get_timer("pod1")
    assert _split(fake.calls[-1].full_url)[0] == "/api/v1/pods/pod1/timer/"
    assert fake.calls[-1].get_method() == "GET"

    client.put_timer("pod1", {"minutesFromNow": 60})
    assert _split(fake.calls[-1].full_url)[0] == "/api/v1/pods/pod1/timer/"
    assert fake.calls[-1].get_method() == "PUT"
    assert json.loads(fake.calls[-1].data.decode("utf-8")) == {"minutesFromNow": 60}

    fake.response = _FakeResponse(b"")
    client.delete_timer("pod1")
    assert _split(fake.calls[-1].full_url)[0] == "/api/v1/pods/pod1/timer/"
    assert fake.calls[-1].get_method() == "DELETE"


def test_schedules_endpoints_use_a_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": []}))
    client = _make_client(monkeypatch, fake)

    client.get_schedules("pod1")
    assert _split(fake.calls[-1].full_url)[0] == "/api/v1/pods/pod1/schedules/"
    assert fake.calls[-1].get_method() == "GET"

    body = {"targetTemperature": 22}
    client.post_schedules("pod1", body)
    assert _split(fake.calls[-1].full_url)[0] == "/api/v1/pods/pod1/schedules/"
    assert fake.calls[-1].get_method() == "POST"
    assert json.loads(fake.calls[-1].data.decode("utf-8")) == body


def test_delete_schedule_uses_the_per_schedule_trailing_slash_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _SingleFakeUrlopen(_FakeResponse(b""))
    client = _make_client(monkeypatch, fake)

    assert client.delete_schedule("pod1", "sched1") is None
    assert _split(fake.calls[-1].full_url)[0] == "/api/v1/pods/pod1/schedules/sched1/"
    assert fake.calls[-1].get_method() == "DELETE"


def test_get_events(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _SingleFakeUrlopen(_json_response({"result": []}))
    client = _make_client(monkeypatch, fake)
    client.get_events("pod1", limit=10)
    req = fake.calls[0]
    assert req.get_method() == "GET"
    path, query = _split(req.full_url)
    assert path == "/api/v2/pods/pod1/events"
    assert query["limit"] == "10"


# --- key resolution wired through the constructor ---------------------------


def test_client_resolves_key_via_resolve_api_key_when_not_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENSIBO_API_KEY", "FROM-ENV")
    client = SensiboClient(min_interval=0)
    fake = _SingleFakeUrlopen(_json_response({"result": []}))
    monkeypatch.setattr(client_module, "urlopen", fake)
    client.get_pods()
    _, query = _split(fake.calls[0].full_url)
    assert query["apiKey"] == "FROM-ENV"
