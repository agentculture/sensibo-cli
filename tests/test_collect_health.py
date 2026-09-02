"""Tests for the collector's health integration (task t5).

The collector is where the three wave-1 packages meet: it builds one
:class:`sensibo.health.Observation` per location per cycle, runs the pure
evaluator, persists everything it returns into the store, and hands the
resulting notifications to a transport.

Two rules hold for every test here, both load-bearing:

* **no network, ever** — the client is a fake and the notify transport is
  injected as a recording callable, so nothing resolves the operator's real
  ``~/.sensibo/.env`` or POSTs to their real webhook;
* **no clock** — every cycle is driven with an explicit ``now``, so a restart
  mid-outage or four cycles of cloud failure replay deterministically.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

import sensibo.cli._commands.collect as collect_cmd
from sensibo.api import ApiError
from sensibo.cli import main
from sensibo.collect import (
    MAX_OWED_ATTEMPTS,
    META_COLLECTOR_OK,
    META_HEALTH_EXTRA,
    META_HEALTH_OWED,
    META_LAST_CYCLE_AT,
    META_LAST_CYCLE_OUTCOME,
    Collector,
)
from sensibo.health import (
    NOTIFY_COLLECTOR_RECOVERED,
    NOTIFY_COLLECTOR_UNHEALTHY,
    NOTIFY_DOWN,
    NOTIFY_RECOVERED,
    STATUS_DOWN,
    STATUS_OK,
    STATUS_UNKNOWN,
    HealthConfig,
)
from sensibo.notify import Outcome
from sensibo.store import Store

NOW = 1_784_000_000.0
HOUR = 3600.0


def _iso(timestamp: float) -> str:
    import datetime

    moment = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pod(*, at: float, is_alive: bool | None = True, sensor_at: float | None = None) -> dict:
    """An Air Pro with one Room Sensor, both stamped with explicit reading times."""
    pod: dict = {
        "id": "pod-airpro",
        "productModel": "airq",
        "room": {"name": "Living Room"},
        "connectionStatus": {"isAlive": is_alive, "lastSeen": {"secondsAgo": 5}},
        "measurements": {"time": {"time": _iso(at)}, "temperature": 23.5, "humidity": 48},
    }
    if sensor_at is not None:
        pod["motionSensors"] = [
            {
                "id": "ms_kitchen01",
                "room": {"name": "Kitchen"},
                "measurements": {"time": {"time": _iso(sensor_at)}, "temperature": 22.0},
            }
        ]
    return pod


def _snapshot(*pods: dict) -> dict:
    return {"status": "success", "result": list(pods)}


class _FakeClient:
    """Returns a canned snapshot, or raises whatever ``failures`` says to."""

    def __init__(self, snapshot: dict | None = None, *, failures: int = 0) -> None:
        self._snapshot = snapshot if snapshot is not None else _snapshot(_pod(at=NOW))
        self.failures = failures
        self.fleet_calls = 0

    def fleet_snapshot(self, fields: str = "*") -> dict:
        self.fleet_calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise ApiError(code=3, message="cloud unreachable", remediation="check the network")
        return self._snapshot

    def get_historical_measurements(self, pod_id: str, days: int = 1) -> dict:
        return {"result": {}}


class _RecordingNotifier:
    """Stands in for :func:`sensibo.notify.send` — records, never delivers.

    ``only`` mirrors the real transport's partial-redelivery parameter: the
    collector passes it when retrying just the legs that still owe delivery.
    """

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.payloads: list = []
        self.onlys: list = []

    def __call__(self, payload, only=None):
        self.payloads.append(payload)
        self.onlys.append(None if only is None else list(only))
        detail = "delivered" if self.ok else "network error: refused"
        return [Outcome("webhook", self.ok, detail)]

    @property
    def kinds(self) -> list[str]:
        return [payload.kind for payload in self.payloads]


class _SplitNotifier:
    """Two transports: ``webhook`` always accepts, ``script`` fails until told not to.

    The partial-delivery case Qodo 12 flagged: one success used to mark the
    whole notification delivered, so the failed leg was never retried.
    """

    def __init__(self) -> None:
        self.script_ok = False
        self.calls: list[tuple[str, list | None]] = []

    def __call__(self, payload, only=None):
        self.calls.append((payload.kind, None if only is None else sorted(only)))
        outcomes = []
        if only is None or "webhook" in only:
            outcomes.append(Outcome("webhook", True, "delivered"))
        if only is None or "script" in only:
            outcomes.append(
                Outcome("script", self.script_ok, "delivered" if self.script_ok else "exit code 1")
            )
        return outcomes


@pytest.fixture(autouse=True)
def _never_the_real_transport(monkeypatch):
    """CLI-driven tests must never resolve the operator's real notify config."""
    notifier = _RecordingNotifier()
    monkeypatch.setattr(collect_cmd, "build_notifier", lambda: notifier)
    return notifier


def _collector(client, store, notifier, **kwargs) -> Collector:
    return Collector(client, store, notifier=notifier, **kwargs)


# --- criterion 1: isAlive is health, meta is written, previous is loaded ----


def test_cycle_records_is_alive_as_health_not_as_a_reading(tmp_path) -> None:
    client = _FakeClient(_snapshot(_pod(at=NOW, is_alive=False)))
    notifier = _RecordingNotifier()
    with Store(db_path=tmp_path / "s.db") as store:
        _collector(client, store, notifier).run_cycle(now=NOW)

        health = store.get_health("pod-airpro")
        assert health is not None
        # isAlive false is down even though the reading time is this instant.
        assert health.status == STATUS_DOWN
        assert health.since == NOW
        # …and it never leaks into the readings table as a measurement field.
        assert "isAlive" not in store.latest_readings("pod-airpro")
        assert "connectionStatus" not in store.latest_readings("pod-airpro")


def test_cycle_writes_last_cycle_meta_on_success(tmp_path) -> None:
    client = _FakeClient(_snapshot(_pod(at=NOW)))
    notifier = _RecordingNotifier()
    with Store(db_path=tmp_path / "s.db") as store:
        _collector(client, store, notifier).run_cycle(now=NOW)

        assert store.get_meta(META_LAST_CYCLE_AT) == repr(NOW)
        assert store.get_meta(META_LAST_CYCLE_OUTCOME) == "ok"
        assert store.get_meta(META_COLLECTOR_OK) == "1"


def test_cycle_writes_failed_outcome_meta_and_a_redacted_reason(tmp_path) -> None:
    client = _FakeClient(failures=1)
    notifier = _RecordingNotifier()
    with Store(db_path=tmp_path / "s.db") as store:
        collector = _collector(client, store, notifier)
        # Exactly one invocation inside the raises block (Sonar S5778).
        with pytest.raises(ApiError):
            collector.run_cycle(now=NOW)

        outcome = store.get_meta(META_LAST_CYCLE_OUTCOME)
        assert outcome is not None
        assert outcome.startswith("failed: ")
        assert "cloud unreachable" in outcome
        assert store.get_meta(META_COLLECTOR_OK) == "0"


def test_room_sensor_observation_carries_its_parent_and_own_reading_time(tmp_path) -> None:
    """A silent Room Sensor under a live pod is down on its OWN stamp."""
    client = _FakeClient(_snapshot(_pod(at=NOW, sensor_at=NOW - 6 * HOUR)))
    notifier = _RecordingNotifier()
    with Store(db_path=tmp_path / "s.db") as store:
        _collector(client, store, notifier).run_cycle(now=NOW)

        assert store.get_health("pod-airpro").status == STATUS_OK
        sensor = store.get_health("ms_kitchen01")
        assert sensor.status == STATUS_DOWN
        assert sensor.parent_pod_id == "pod-airpro"
    assert notifier.kinds == [NOTIFY_DOWN]
    assert notifier.payloads[0].location == "ms_kitchen01"


def test_health_extras_are_persisted_and_reloaded(tmp_path) -> None:
    """The debounce fields the health table has no column for survive a cycle."""
    client = _FakeClient(_snapshot(_pod(at=NOW)))
    notifier = _RecordingNotifier()
    with Store(db_path=tmp_path / "s.db") as store:
        collector = _collector(client, store, notifier)
        collector.run_cycle(now=NOW)
        extras = json.loads(store.get_meta(META_HEALTH_EXTRA))
        assert extras["pod-airpro"]["ok_streak"] == 1
        assert extras["pod-airpro"]["day_key"] == _iso(NOW)[:10]

        # a second cycle reloads the map from the store and advances the streak
        collector.run_cycle(now=NOW + 90)
        extras = json.loads(store.get_meta(META_HEALTH_EXTRA))
        assert extras["pod-airpro"]["ok_streak"] == 2

        loaded = collector.load_health_states()
        assert loaded["pod-airpro"].ok_streak == 2
        assert loaded["pod-airpro"].status == STATUS_OK


# --- criterion 2: persist the transition first, notify after, restart-safe --


def test_transition_is_persisted_and_notified_only_after_a_successful_send(tmp_path) -> None:
    client = _FakeClient(_snapshot(_pod(at=NOW - 6 * HOUR)))
    notifier = _RecordingNotifier()
    with Store(db_path=tmp_path / "s.db") as store:
        _collector(client, store, notifier).run_cycle(now=NOW)

        transitions = store.list_transitions("pod-airpro")
        assert [t.to_status for t in transitions] == [STATUS_DOWN]
        assert transitions[0].notified_at == NOW

        notes = store.list_notifications()
        assert [(n.kind, n.transport, n.outcome) for n in notes] == [
            (NOTIFY_DOWN, "webhook", "delivered")
        ]
    assert notifier.kinds == [NOTIFY_DOWN]


def test_failed_delivery_leaves_the_transition_owed_and_retries_next_cycle(tmp_path) -> None:
    client = _FakeClient(_snapshot(_pod(at=NOW - 6 * HOUR)))
    failing = _RecordingNotifier(ok=False)
    with Store(db_path=tmp_path / "s.db") as store:
        collector = _collector(client, store, failing)
        collector.run_cycle(now=NOW)

        transition = store.list_transitions("pod-airpro")[0]
        assert transition.notified_at is None  # still owed
        assert store.list_notifications()[0].outcome.startswith("failed:")
        assert json.loads(store.get_meta(META_HEALTH_OWED))

        # next cycle: a working transport clears the debt exactly once
        working = _RecordingNotifier()
        collector = _collector(client, store, working)
        collector.run_cycle(now=NOW + 90)

        assert working.kinds == [NOTIFY_DOWN]
        assert store.list_transitions("pod-airpro")[0].notified_at == NOW + 90
        assert json.loads(store.get_meta(META_HEALTH_OWED)) == []


def test_restart_mid_outage_sends_one_down_and_one_recovery(tmp_path) -> None:
    """The whole point of persisting HealthState: a restart is not an alert."""
    down_snapshot = _snapshot(_pod(at=NOW - 6 * HOUR))
    first = _RecordingNotifier()
    second = _RecordingNotifier()
    third = _RecordingNotifier()
    config = HealthConfig(cooldown_seconds=0.0)  # cooldown must NOT be what saves us
    with Store(db_path=tmp_path / "s.db") as store:
        _collector(_FakeClient(down_snapshot), store, first, health_config=config).run_cycle(
            now=NOW
        )
        assert first.kinds == [NOTIFY_DOWN]

        # daemon restarted: a brand-new Collector over the same store, still down
        _collector(_FakeClient(down_snapshot), store, second, health_config=config).run_cycle(
            now=NOW + 90
        )
        assert second.kinds == []  # no second down alert

        # the pod starts reporting again; recovery holds for 2 cycles, then fires
        client = _FakeClient(_snapshot(_pod(at=NOW + 180)))
        collector = _collector(client, store, third, health_config=config)
        collector.run_cycle(now=NOW + 180)
        assert third.kinds == []  # hold not yet satisfied
        client._snapshot = _snapshot(_pod(at=NOW + 270))
        collector.run_cycle(now=NOW + 270)

        assert third.kinds == [NOTIFY_RECOVERED]
        assert store.get_health("pod-airpro").status == STATUS_OK
        kinds = [note.kind for note in store.list_notifications()]
        assert kinds.count(NOTIFY_DOWN) == 1
        assert kinds.count(NOTIFY_RECOVERED) == 1


# --- criterion 4: a collector outage is one alert, not N sensor alerts ------


def test_three_failed_cycles_then_success_is_one_unhealthy_and_one_recovered(tmp_path) -> None:
    client = _FakeClient(_snapshot(_pod(at=NOW + 400, sensor_at=NOW + 400)), failures=3)
    notifier = _RecordingNotifier()
    with Store(db_path=tmp_path / "s.db") as store:
        collector = _collector(client, store, notifier)
        for index in range(3):
            with pytest.raises(ApiError):
                collector.run_cycle(now=NOW + index * 90)
        result, _pods = collector.run_cycle(now=NOW + 400)

        assert notifier.kinds == [NOTIFY_COLLECTOR_UNHEALTHY, NOTIFY_COLLECTOR_RECOVERED]
        assert [t.to_status for t in store.list_transitions()] == [STATUS_OK, STATUS_OK]
        assert not [t for t in store.list_transitions() if t.to_status == STATUS_DOWN]
        assert result.health_ok == 2


def test_collector_failure_marks_known_locations_unknown_not_down(tmp_path) -> None:
    client = _FakeClient(_snapshot(_pod(at=NOW)))
    notifier = _RecordingNotifier()
    with Store(db_path=tmp_path / "s.db") as store:
        collector = _collector(client, store, notifier)
        collector.run_cycle(now=NOW)
        client.failures = 1
        with pytest.raises(ApiError):
            collector.run_cycle(now=NOW + 90)

        assert store.get_health("pod-airpro").status == STATUS_UNKNOWN
    assert notifier.kinds == [NOTIFY_COLLECTOR_UNHEALTHY]


# --- criterion 5: the counts reach CycleResult, stdout, and --json ----------


def test_cycle_result_carries_health_counts(tmp_path) -> None:
    client = _FakeClient(_snapshot(_pod(at=NOW - 6 * HOUR, sensor_at=NOW)))
    notifier = _RecordingNotifier()
    with Store(db_path=tmp_path / "s.db") as store:
        result, _pods = _collector(client, store, notifier).run_cycle(now=NOW)

    assert result.health_down == 1  # the pod
    assert result.health_unknown == 1  # its sensor, sheltered by the down parent
    assert result.health_ok == 0
    assert result.notifications_sent == 1
    assert result.notifications_suppressed == 0
    assert result.to_dict()["health"] == {
        "ok": 0,
        "down": 1,
        "unknown": 1,
        "notifications_sent": 1,
        "notifications_suppressed": 0,
    }


# --- CLI wiring ------------------------------------------------------------


@pytest.fixture
def patched_client(monkeypatch):
    def _install(client: _FakeClient) -> _FakeClient:
        monkeypatch.setattr(collect_cmd, "build_client", lambda: client)
        return client

    return _install


def test_collect_json_summary_carries_health(patched_client, capsys, tmp_path) -> None:
    patched_client(_FakeClient(_snapshot(_pod(at=NOW))))
    rc = main(["collect", "--once", "--db", str(tmp_path / "s.db"), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["health"]) == {
        "ok",
        "down",
        "unknown",
        "notifications_sent",
        "notifications_suppressed",
    }


def test_collect_text_summary_carries_health(patched_client, capsys, tmp_path) -> None:
    patched_client(_FakeClient(_snapshot(_pod(at=NOW))))
    rc = main(["collect", "--once", "--db", str(tmp_path / "s.db")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "health:" in out
    assert "notification" in out


def test_collect_once_still_exits_two_on_api_error_after_recording(
    patched_client, capsys, tmp_path
) -> None:
    db = tmp_path / "s.db"
    patched_client(_FakeClient(failures=1))
    rc = main(["collect", "--once", "--db", str(db)])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "Traceback" not in err

    with Store(db_path=db) as store:
        assert store.get_meta(META_LAST_CYCLE_OUTCOME).startswith("failed:")
        assert store.get_meta(META_COLLECTOR_OK) == "0"


def test_collect_daemon_survives_an_api_error_and_keeps_the_interval(
    patched_client, monkeypatch, capsys, tmp_path
) -> None:
    client = patched_client(_FakeClient(_snapshot(_pod(at=NOW)), failures=2))
    waits: list[float] = []

    def _sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(collect_cmd, "_sleep", _sleep)

    rc = main(["collect", "--daemon", "--interval", "60", "--db", str(tmp_path / "s.db")])
    assert rc == 0  # the daemon did not die on the ApiError
    assert client.fleet_calls == 3  # two failures, then a good cycle
    assert waits == [60.0, 60.0, 60.0]  # the normal interval, never a crash-loop
    err = capsys.readouterr().err
    assert "cloud unreachable" in err
    assert "Traceback" not in err


def test_collect_rejects_a_bad_health_threshold_as_a_user_error(
    patched_client, monkeypatch, capsys, tmp_path
) -> None:
    patched_client(_FakeClient(_snapshot(_pod(at=NOW))))
    monkeypatch.setenv("SENSIBO_HEALTH_DOWN_AFTER", "not-a-number")
    rc = main(["collect", "--once", "--db", str(tmp_path / "s.db")])
    assert rc == 1  # user error, not a traceback
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "SENSIBO_HEALTH_DOWN_AFTER" in err


# --- Qodo review fixes (PR #11) --------------------------------------------


class _CountingConnection:
    """Counts committed transactions — ``sqlite3.Connection`` cannot be patched.

    Every ``with self._conn:`` block in the store is exactly one transaction, so
    counting ``__exit__`` counts commits.
    """

    def __init__(self, wrapped: sqlite3.Connection) -> None:
        self.wrapped = wrapped
        self.transactions = 0

    def __enter__(self):
        return self.wrapped.__enter__()

    def __exit__(self, *exc_info):
        self.transactions += 1
        return self.wrapped.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


def test_owed_notification_is_dropped_after_the_attempt_cap(tmp_path, capsys) -> None:
    """Q3: the owed queue retries once per cycle and gives up, it does not spin forever.

    No exponential backoff is expected — the cycle cadence is the spacing — but
    the debt must expire, be logged once, and leave an audit row behind.
    """
    client = _FakeClient(_snapshot(_pod(at=NOW - 6 * HOUR)))
    failing = _RecordingNotifier(ok=False)
    logged: list[str] = []
    with Store(db_path=tmp_path / "s.db") as store:
        collector = Collector(client, store, notifier=failing, log=logged.append)
        for index in range(MAX_OWED_ATTEMPTS):
            collector.run_cycle(now=NOW + index * 90)

        assert json.loads(store.get_meta(META_HEALTH_OWED)) == []
        dropped = [n for n in store.list_notifications() if n.outcome.startswith("dropped after")]
        assert len(dropped) == 1
        assert dropped[0].outcome == f"dropped after {MAX_OWED_ATTEMPTS} attempts"
        assert dropped[0].kind == NOTIFY_DOWN
        assert sum("giving up" in line for line in logged) == 1


def test_health_rows_are_persisted_in_one_transaction_not_one_per_location(
    tmp_path, monkeypatch
) -> None:
    """Q7: five locations must not mean five health commits."""
    pods = []
    for index in range(5):
        pod = _pod(at=NOW)
        pod["id"] = f"pod-{index}"
        pods.append(pod)
    client = _FakeClient(_snapshot(*pods))
    notifier = _RecordingNotifier()

    def _forbidden(*args, **kwargs):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("set_health must not be called per location during a cycle")

    monkeypatch.setattr(Store, "set_health", _forbidden)
    with Store(db_path=tmp_path / "s.db") as store:
        _collector(client, store, notifier).run_cycle(now=NOW)
        assert len(store.list_health()) == 5

    # …and the bulk method really is one transaction, not five.
    with Store(db_path=tmp_path / "bulk.db") as store:
        counting = _CountingConnection(store._conn)
        store._conn = counting
        store.set_health_many((f"pod-{index}", STATUS_OK, NOW, NOW, None) for index in range(5))
        assert counting.transactions == 1
        assert len(store.list_health()) == 5
        store._conn = counting.wrapped


def test_a_crash_after_persistence_still_delivers_the_alert_next_cycle(tmp_path) -> None:
    """Q11: transitions and the owed debt are committed before any transport runs.

    The old order committed the *health state* first, so a process exit before
    the transition was written left the location recorded as down with no edge
    left for the next cycle to re-announce — the alert was simply lost.
    """
    client = _FakeClient(_snapshot(_pod(at=NOW - 6 * HOUR)))
    crashing = _RecordingNotifier()

    class _Crash(RuntimeError):
        pass

    def _die(*args, **kwargs):
        raise _Crash("process exited between persistence and delivery")

    db = tmp_path / "s.db"
    with Store(db_path=db) as store:
        collector = _collector(client, store, crashing)
        collector._dispatch = _die
        with pytest.raises(_Crash):
            collector.run_cycle(now=NOW)

    # The process is gone; nothing was delivered. Reopen the store as a restart.
    with Store(db_path=db) as store:
        transition = store.list_transitions("pod-airpro")[0]
        assert transition.to_status == STATUS_DOWN
        assert transition.notified_at is None
        assert len(json.loads(store.get_meta(META_HEALTH_OWED))) == 1

        working = _RecordingNotifier()
        _collector(_FakeClient(_snapshot(_pod(at=NOW - 6 * HOUR))), store, working).run_cycle(
            now=NOW + 90
        )
        assert working.kinds == [NOTIFY_DOWN]
        assert store.list_transitions("pod-airpro")[0].notified_at == NOW + 90
        assert json.loads(store.get_meta(META_HEALTH_OWED)) == []


def test_a_partly_delivered_alert_retries_only_the_failed_transport(tmp_path) -> None:
    """Q12: a webhook success must not cancel the script leg's debt."""
    client = _FakeClient(_snapshot(_pod(at=NOW - 6 * HOUR)))
    split = _SplitNotifier()
    with Store(db_path=tmp_path / "s.db") as store:
        collector = _collector(client, store, split)
        collector.run_cycle(now=NOW)

        # The webhook accepted it, so the transition is stamped…
        assert store.list_transitions("pod-airpro")[0].notified_at == NOW
        # …but the script still owes delivery, and only the script.
        owed = json.loads(store.get_meta(META_HEALTH_OWED))
        assert [entry["transports"] for entry in owed] == [["script"]]

        collector.run_cycle(now=NOW + 90)
        assert split.calls[-1] == (NOTIFY_DOWN, ["script"])  # webhook not re-sent

        split.script_ok = True
        collector.run_cycle(now=NOW + 180)
        assert json.loads(store.get_meta(META_HEALTH_OWED)) == []

        by_transport = [(n.transport, n.outcome) for n in store.list_notifications()]
        assert ("script", "delivered") in by_transport
        assert by_transport.count(("webhook", "delivered")) == 1


def test_a_collector_error_is_scrubbed_before_it_reaches_a_notification(tmp_path) -> None:
    """Q17: the raw ApiError text lands in an outbound webhook body — scrub it first."""

    class _LeakyClient(_FakeClient):
        def fleet_snapshot(self, fields: str = "*") -> dict:
            raise ApiError(
                code=3,
                message=(
                    "GET https://home.sensibo.com/api/v2/users/me/pods"
                    "?fields=*&apiKey=SECRET123 failed: HTTP 500"
                ),
                remediation="retry later",
            )

    notifier = _RecordingNotifier()
    with Store(db_path=tmp_path / "s.db") as store:
        collector = _collector(_LeakyClient(), store, notifier)
        with pytest.raises(ApiError):
            collector.run_cycle(now=NOW)

    assert notifier.kinds == [NOTIFY_COLLECTOR_UNHEALTHY]
    message = notifier.payloads[0].message
    assert "SECRET123" not in message
    assert "apiKey=REDACTED" in message
