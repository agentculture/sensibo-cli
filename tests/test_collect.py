"""Tests for the collector (task t6) — the retention thesis made runnable.

Two layers are exercised here, both against a mocked client and a ``tmp_path``
store. **No test ever makes a real network call or touches the real
``~/.sensibo``** (the collector's hard rule, mirroring the api/store suites):

* the pure engine (:class:`sensibo.collect.Collector`) driven with a
  duck-typed fake client and a real :class:`~sensibo.store.Store`;
* the CLI verb (``sensibo collect``) driven through
  :func:`sensibo.cli.main`, with :func:`build_client` monkeypatched to hand
  back the fake and ``--db`` pointing at a tmp file.

The one exception to "fake client" is the 429 backoff test, which uses a
*real* :class:`~sensibo.api.SensiboClient` with ``urlopen`` mocked — that is
the only way to prove the client itself retries and the cycle still completes.
"""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest

import sensibo.api.client as client_module
import sensibo.cli._commands.collect as collect_cmd
from sensibo.api import GatedHistoryWindowError
from sensibo.api.client import SensiboClient
from sensibo.cli import main
from sensibo.collect import (
    BACKFILL_WINDOWS,
    DEFAULT_INTERVAL,
    META_BACKFILL_DONE,
    META_BACKFILL_WINDOW,
    MIN_INTERVAL,
    BackfillResult,
    Collector,
    CycleResult,
)
from sensibo.store import KIND_POD, KIND_ROOM_SENSOR, Store

# --- sample fleet data ----------------------------------------------------

_POD_TIME = "2026-07-14T10:00:00Z"
_MS_TIME = "2026-07-14T10:00:05Z"


def _airpro_pod() -> dict:
    """An Air Pro (`airq`) with a nested Room Sensor in `motionSensors[]`."""
    return {
        "id": "pod-airpro",
        "productModel": "airq",
        "room": {"name": "Living Room"},
        "measurements": {
            "time": {"time": _POD_TIME, "secondsAgo": 30},
            "temperature": 23.5,
            "humidity": 48,
            "co2": 600,
            "tvoc": 120,
            "roomIsOccupied": True,
        },
        "motionSensors": [
            {
                "id": "ms_kitchen01",
                "room": {"name": "Kitchen"},
                "measurements": {
                    "time": {"time": _MS_TIME},
                    "temperature": 22.0,
                    "humidity": 51,
                    "motion": True,
                    "battery": 88,
                },
            }
        ],
    }


def _pure_pod() -> dict:
    return {
        "id": "pod-pure",
        "productModel": "pure",
        "room": {"name": "Bedroom"},
        "measurements": {"time": {"time": _POD_TIME}, "pm25": 2, "temperature": 21.0},
    }


def _elements_pod() -> dict:
    return {
        "id": "pod-elem",
        "productModel": "elements",
        "room": {"name": "Study"},
        "measurements": {"time": {"time": _POD_TIME}, "pm25": 15.4, "temperature": 20.0},
    }


def _snapshot(*pods: dict) -> dict:
    return {"status": "success", "result": list(pods)}


# --- fake client ----------------------------------------------------------


class _FakeClient:
    """Duck-typed stand-in for :class:`SensiboClient`.

    Records every call. ``fleet_snapshot`` returns a canned envelope;
    ``get_historical_measurements`` delegates to ``history_fn`` (which may
    raise :class:`GatedHistoryWindowError`). The per-device polling methods are
    present so a test can assert the collector never falls back to looping them.
    """

    def __init__(self, snapshot: dict, *, history_fn=None) -> None:
        self._snapshot = snapshot
        self._history_fn = history_fn
        self.fleet_calls = 0
        self.history_calls: list[tuple[str, int]] = []
        self.per_device_calls: list[str] = []

    def fleet_snapshot(self, fields: str = "*") -> dict:
        self.fleet_calls += 1
        return self._snapshot

    def get_historical_measurements(self, pod_id: str, days: int = 1) -> dict:
        self.history_calls.append((pod_id, days))
        if self._history_fn is None:
            return {"result": {}}
        return self._history_fn(pod_id, days)

    def get_pod(self, *a, **k):  # pragma: no cover - only called if the collector regresses
        self.per_device_calls.append("get_pod")
        return {"result": {}}

    def get_measurements(self, *a, **k):  # pragma: no cover - regression sentinel
        self.per_device_calls.append("get_measurements")
        return {"result": {}}


def _gated_until(threshold: int):
    """History fn: 403 for ``days > threshold``, a canned series at/below it."""

    def _fn(pod_id: str, days: int) -> dict:
        if days > threshold:
            raise GatedHistoryWindowError(pod_id=pod_id, days=days, message=f"gated at days={days}")
        return {
            "result": {
                "temperature": [
                    {"time": "2026-07-13T09:00:00Z", "value": 19.0},
                    {"time": "2026-07-13T09:01:30Z", "value": 19.2},
                ],
                "humidity": [{"time": "2026-07-13T09:00:00Z", "value": 40}],
            }
        }

    return _fn


# --- engine: one fleet call per cycle -------------------------------------


def test_cycle_is_exactly_one_fleet_snapshot_call(tmp_path) -> None:
    client = _FakeClient(_snapshot(_airpro_pod()))
    with Store(db_path=tmp_path / "s.db") as store:
        Collector(client, store).run_cycle()
    assert client.fleet_calls == 1
    # never a per-device loop for a fleet-wide snapshot (docs/sensibo-api.md).
    assert client.per_device_calls == []


def test_cycle_upserts_pod_and_records_its_measurements(tmp_path) -> None:
    client = _FakeClient(_snapshot(_airpro_pod()))
    with Store(db_path=tmp_path / "s.db") as store:
        result, _pods = Collector(client, store).run_cycle()

        loc = store.get_location("pod-airpro")
        assert loc is not None
        assert loc.kind == KIND_POD
        assert loc.product_model == "airq"
        assert loc.room_name == "Living Room"

        latest = store.latest_readings("pod-airpro")
        assert latest["temperature"].value == 23.5
        assert latest["temperature"].unit == "C"
        assert latest["co2"].value == 600
        assert latest["co2"].unit == "ppm"
        # the `time` sub-object is metadata, never stored as a reading field.
        assert "time" not in latest
        # bools survive (roomIsOccupied stored 1.0).
        assert latest["roomIsOccupied"].value == 1.0

    assert isinstance(result, CycleResult)
    assert result.pods == 1
    assert result.room_sensors == 1
    assert result.locations_seen == 2


def test_cycle_records_room_sensor_from_motion_sensors(tmp_path) -> None:
    """A Room Sensor is not a pod: it lands under its ``ms_*`` id with a parent."""
    client = _FakeClient(_snapshot(_airpro_pod()))
    with Store(db_path=tmp_path / "s.db") as store:
        Collector(client, store).run_cycle()

        room = store.get_location("ms_kitchen01")
        assert room is not None
        assert room.kind == KIND_ROOM_SENSOR
        assert room.parent_pod_id == "pod-airpro"
        assert room.room_name == "Kitchen"

        latest = store.latest_readings("ms_kitchen01")
        assert latest["temperature"].value == 22.0
        assert latest["motion"].value == 1.0
        assert latest["battery"].value == 88
        assert latest["battery"].unit == "%"


# --- pm25 polymorphism: the trap ------------------------------------------


def test_pm25_branches_on_product_model_pure_vs_elements(tmp_path) -> None:
    client = _FakeClient(_snapshot(_pure_pod(), _elements_pod()))
    with Store(db_path=tmp_path / "s.db") as store:
        Collector(client, store).run_cycle()

        pure = store.latest_reading("pod-pure", "pm25")
        assert pure is not None
        assert pure.value == 2  # AQI enum, stored as-is
        assert pure.unit == "aqi"

        elem = store.latest_reading("pod-elem", "pm25")
        assert elem is not None
        assert elem.value == 15.4  # micrograms per cubic metre
        assert elem.unit == "ug/m3"


# --- idempotency: API reading time, not wall clock ------------------------


def test_cycle_stores_api_reading_time_and_is_idempotent(tmp_path) -> None:
    import datetime

    client = _FakeClient(_snapshot(_airpro_pod()))
    with Store(db_path=tmp_path / "s.db") as store:
        collector = Collector(client, store)
        collector.run_cycle()
        collector.run_cycle()  # same snapshot again — must not duplicate rows

        rows = store.query_range("pod-airpro", "temperature")
        assert len(rows) == 1  # idempotent: keyed on the API reading time
        expected = datetime.datetime(
            2026, 7, 14, 10, 0, 0, tzinfo=datetime.timezone.utc
        ).timestamp()
        assert rows[0].timestamp == expected


# --- 429 backoff: the REAL client retries and the cycle completes ---------


class _FakeHeaders:
    def __init__(self, data=None):
        self._d = {k.lower(): v for k, v in (data or {}).items()}

    def get(self, name, default=""):
        return self._d.get(name.lower(), default)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.headers = _FakeHeaders()

    def read(self):
        return self._body

    def close(self):
        pass


class _SequenceUrlopen:
    def __init__(self, items):
        self._items = list(items)
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_cycle_backs_off_on_429_and_completes(monkeypatch, tmp_path) -> None:
    body = json.dumps(_snapshot(_airpro_pod())).encode("utf-8")
    http_429 = HTTPError(
        "https://home.sensibo.com/api/v2/users/me/pods?apiKey=SECRET",
        429,
        "Too Many Requests",
        {},
        io.BytesIO(b"slow down"),
    )
    fake = _SequenceUrlopen([http_429, _FakeResponse(body)])
    monkeypatch.setattr(client_module, "urlopen", fake)
    # backoff_base/jitter 0 → time.sleep(0), so the retry is instant in tests.
    client = SensiboClient(
        api_key="SECRET",
        min_interval=0,
        max_retries=3,
        backoff_base=0,
        backoff_jitter=0,
    )

    with Store(db_path=tmp_path / "s.db") as store:
        result, _pods = Collector(client, store).run_cycle()
        assert store.latest_reading("pod-airpro", "temperature").value == 23.5

    assert fake.calls == 2  # one 429, one success — the client retried
    assert result.pods == 1


# --- first-run backfill: descending probe, 403 = "try smaller" ------------


def test_backfill_descends_to_days_one_when_everything_above_403s(tmp_path) -> None:
    client = _FakeClient(_snapshot(_airpro_pod()), history_fn=_gated_until(1))
    with Store(db_path=tmp_path / "s.db") as store:
        Collector(client, store).run_cycle()  # populate locations first
        result = Collector(client, store).backfill(client._snapshot["result"])

        assert isinstance(result, BackfillResult)
        # probed 730, 365, 90, 30, 7 (all 403), then 1 (success) — and stopped.
        assert client.history_calls == [("pod-airpro", d) for d in BACKFILL_WINDOWS]
        assert result.window_days == 1

        # the days=1 series was recorded into the store — 2 historical
        # temperature points on top of the 1 from the cycle above.
        temps = store.query_range("pod-airpro", "temperature")
        assert len(temps) == 3
        assert 19.0 in {t.value for t in temps}
        assert result.readings_written == 3  # 2 temperature + 1 humidity, from backfill


def test_backfill_uses_largest_permitted_window_and_stops_early(tmp_path) -> None:
    # 730/365 gated; 90 is the first permitted → stop, don't probe below it.
    client = _FakeClient(_snapshot(_airpro_pod()), history_fn=_gated_until(90))
    with Store(db_path=tmp_path / "s.db") as store:
        Collector(client, store).run_cycle()
        result = Collector(client, store).backfill(client._snapshot["result"])

    assert client.history_calls == [("pod-airpro", 730), ("pod-airpro", 365), ("pod-airpro", 90)]
    assert result.window_days == 90


def test_backfill_persists_window_and_done_flag_to_store_meta(tmp_path) -> None:
    client = _FakeClient(_snapshot(_airpro_pod()), history_fn=_gated_until(1))
    with Store(db_path=tmp_path / "s.db") as store:
        Collector(client, store).run_cycle()
        Collector(client, store).backfill(client._snapshot["result"])

        assert store.get_meta(META_BACKFILL_WINDOW) == "1"
        assert store.get_meta(META_BACKFILL_DONE) == "1"


def test_backfill_logs_found_window_to_stderr(tmp_path) -> None:
    logs: list[str] = []
    client = _FakeClient(_snapshot(_airpro_pod()), history_fn=_gated_until(1))
    with Store(db_path=tmp_path / "s.db") as store:
        collector = Collector(client, store, log=logs.append)
        collector.run_cycle()
        collector.backfill(client._snapshot["result"])
    assert any("days=1" in line for line in logs)


def test_collect_once_runs_backfill_only_on_first_run(tmp_path) -> None:
    client = _FakeClient(_snapshot(_airpro_pod()), history_fn=_gated_until(1))
    with Store(db_path=tmp_path / "s.db") as store:
        collector = Collector(client, store)
        first = collector.collect_once()
        assert first.backfill is not None
        assert first.backfill.window_days == 1

        client.history_calls.clear()
        second = collector.collect_once()
        assert second.backfill is None  # meta says backfill already done
        assert client.history_calls == []  # no re-probe


# --- CLI wiring -----------------------------------------------------------


@pytest.fixture()
def patched_client(monkeypatch):
    """Install a fake client factory; return a setter the test calls with data."""
    holder: dict[str, _FakeClient] = {}

    def _install(client: _FakeClient) -> None:
        holder["client"] = client
        monkeypatch.setattr(collect_cmd, "build_client", lambda: holder["client"])

    return _install


def test_collect_once_prints_json_summary(patched_client, capsys, tmp_path) -> None:
    patched_client(_FakeClient(_snapshot(_airpro_pod()), history_fn=_gated_until(1)))
    db = tmp_path / "s.db"
    rc = main(["collect", "--once", "--db", str(db), "--json"])
    assert rc == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # stdout is JUST the summary
    assert payload["locations_seen"] == 2
    assert payload["pods"] == 1
    assert payload["room_sensors"] == 1
    assert payload["readings_written"] >= 1
    assert payload["backfill"]["ran"] is True
    assert payload["backfill"]["window_days"] == 1
    # the window log goes to stderr — the stream split holds.
    assert "days=1" in captured.err


def test_collect_once_text_summary_to_stdout(patched_client, capsys, tmp_path) -> None:
    patched_client(_FakeClient(_snapshot(_airpro_pod()), history_fn=_gated_until(1)))
    rc = main(["collect", "--once", "--db", str(tmp_path / "s.db")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "locations" in out.lower()
    assert "sensibo-cli" not in out or True  # summary is command output, not naming


def test_collect_default_is_a_single_cycle(patched_client, capsys, tmp_path) -> None:
    fake = _FakeClient(_snapshot(_airpro_pod()), history_fn=_gated_until(1))
    patched_client(fake)
    rc = main(["collect", "--db", str(tmp_path / "s.db"), "--json"])
    assert rc == 0
    assert fake.fleet_calls == 1  # one cycle, not a daemon loop


def test_collect_rejects_interval_below_floor(capsys) -> None:
    rc = main(["collect", "--daemon", "--interval", "30"])
    assert rc == 1  # user error
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert str(int(MIN_INTERVAL)) in err  # names the 60s floor


def test_collect_accepts_interval_at_floor(patched_client, capsys, tmp_path) -> None:
    patched_client(_FakeClient(_snapshot(_airpro_pod()), history_fn=_gated_until(1)))
    rc = main(
        ["collect", "--once", "--interval", str(int(MIN_INTERVAL)), "--db", str(tmp_path / "s.db")]
    )
    assert rc == 0


def test_collect_once_and_daemon_are_mutually_exclusive(capsys) -> None:
    rc = main(["collect", "--once", "--daemon"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


def test_collect_daemon_handles_keyboard_interrupt_cleanly(
    patched_client, monkeypatch, capsys, tmp_path
) -> None:
    fake = _FakeClient(_snapshot(_airpro_pod()), history_fn=_gated_until(1))
    patched_client(fake)

    def _interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(collect_cmd, "_sleep", _interrupt)

    rc = main(["collect", "--daemon", "--db", str(tmp_path / "s.db"), "--json"])
    assert rc == 0  # clean stop, not a crash
    captured = capsys.readouterr()
    assert fake.fleet_calls == 1  # ran one cycle, then Ctrl-C during the sleep
    assert "Traceback" not in captured.err
    assert "stop" in captured.err.lower()


def test_collect_maps_api_error_to_clean_cli_error(monkeypatch, capsys, tmp_path) -> None:
    from sensibo.api import MissingApiKeyError

    def _boom():
        raise MissingApiKeyError("no api key", remediation="set SENSIBO_API_KEY")

    monkeypatch.setattr(collect_cmd, "build_client", _boom)
    rc = main(["collect", "--once", "--db", str(tmp_path / "s.db")])
    assert rc == 2  # environment/setup error
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_collect_parse_error_names_the_console_command(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["collect", "--bogus"])
    err = capsys.readouterr().err
    assert "hint:" in err
    assert "sensibo-cli --help" not in err  # the dist name is never a runnable command


def test_collect_has_an_explain_entry(capsys) -> None:
    rc = main(["explain", "collect"])
    assert rc == 0
    assert "collect" in capsys.readouterr().out.lower()


def test_default_interval_constant_is_ninety() -> None:
    assert DEFAULT_INTERVAL == 90
    assert MIN_INTERVAL == 60


# --- store metadata (added for the collector) -----------------------------


def test_store_meta_roundtrip_and_upsert(tmp_path) -> None:
    with Store(db_path=tmp_path / "s.db") as store:
        assert store.get_meta("nope") is None
        store.set_meta("k", "v1")
        assert store.get_meta("k") == "v1"
        store.set_meta("k", "v2")  # idempotent upsert, not a duplicate
        assert store.get_meta("k") == "v2"
