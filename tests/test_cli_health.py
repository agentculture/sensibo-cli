"""Tests for ``sensibo query health`` and ``sensibo doctor``'s collector_heartbeat
check (task t6).

Written test-first: these fail against a CLI with no ``query health`` verb and
no ``collector_heartbeat`` doctor check.

Hard rule enforced throughout (mirrors ``tests/test_query.py``): ``query
health`` must **never** touch the network — it answers from SQLite alone.
"""

from __future__ import annotations

import datetime
import json
import socket
import time
from pathlib import Path

import pytest

from sensibo.cli import main
from sensibo.health import EXECUTION_LOCAL
from sensibo.store import Store

# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blow up on any socket use — proves `query health` never touches the network."""

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise OSError("network disabled for this test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


_T_DOWN = 1_000_000.0
_T_RECOVER = _T_DOWN + 3 * 3600.0  # down for 3 hours


def _seed_outage_store(db_path: Path) -> None:
    """A location that went down at T and recovered at T+3h."""
    with Store(db_path=db_path) as store:
        store.upsert_location(
            "pod-1",
            kind="pod",
            product_model="elements",
            room_name="Living Room",
            seen_at=_T_RECOVER,
        )
        store.set_alias("pod-1", "Den")
        store.record_transition("pod-1", None, "ok", at=_T_DOWN - 3600.0)
        store.record_transition("pod-1", "ok", "down", at=_T_DOWN)
        store.record_transition("pod-1", "down", "ok", at=_T_RECOVER)
        store.set_health("pod-1", status="ok", since=_T_RECOVER, last_ok=_T_RECOVER)
        store.set_meta("last_cycle_at", str(_T_RECOVER))
        store.set_meta("last_cycle_outcome", "ok")


# --- query health --------------------------------------------------------


def test_health_empty_store_errors_with_collect_remediation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    rc = main(["query", "health", "--db", str(db)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "sensibo collect" in err


def test_health_all_locations_json_renders_outage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_outage_store(db)

    rc = main(["query", "health", "--db", str(db), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["execution"] == EXECUTION_LOCAL
    assert payload["collector"]["last_cycle_outcome"] == "ok"

    locations = payload["locations"]
    assert len(locations) == 1
    loc = locations[0]
    assert loc["location_id"] == "pod-1"
    assert loc["status"] == "ok"
    assert loc["last_ok"] != "never"

    assert len(loc["outages"]) == 1
    outage = loc["outages"][0]
    assert outage["duration_seconds"] == pytest.approx(3 * 3600.0)
    assert outage["start"]
    assert outage["end"]


def test_health_resolves_location_by_alias(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_outage_store(db)

    rc = main(["query", "health", "Den", "--db", str(db), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["locations"]) == 1
    assert payload["locations"][0]["location_id"] == "pod-1"


def test_health_resolves_location_by_room_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_outage_store(db)

    rc = main(["query", "health", "Living Room", "--db", str(db), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["locations"][0]["location_id"] == "pod-1"


def test_health_unknown_location_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_outage_store(db)

    rc = main(["query", "health", "nonexistent", "--db", str(db)])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_health_text_output_shows_start_end_duration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_outage_store(db)

    rc = main(["query", "health", "--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "pod-1" in out
    assert "start=" in out
    assert "end=" in out
    assert "duration_seconds=" in out
    assert EXECUTION_LOCAL in out


def test_health_since_filters_transitions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_outage_store(db)
    since_iso = datetime.datetime.fromtimestamp(_T_DOWN + 1.0, tz=datetime.timezone.utc).isoformat()

    rc = main(["query", "health", "--db", str(db), "--since", since_iso, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    loc = payload["locations"][0]
    # Only the recovery transition (down->ok) remains after the outage start.
    assert len(loc["transitions"]) == 1
    assert loc["transitions"][0]["to_status"] == "ok"
    # An outage requires both ends; with the down-transition filtered out there
    # is nothing closed to report.
    assert loc["outages"] == []


def test_health_never_touches_the_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_outage_store(db)
    # If this reaches a socket call, block_network raises and the test fails.
    rc = main(["query", "health", "--db", str(db), "--json"])
    assert rc == 0


# --- doctor collector_heartbeat -------------------------------------------


def test_doctor_reports_absent_heartbeat_unhealthy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sensibo.db"
    rc = main(["doctor", "--db", str(db), "--json"])
    payload = json.loads(capsys.readouterr().out)
    checks = {c["id"]: c for c in payload["checks"]}
    assert "collector_heartbeat" in checks
    heartbeat = checks["collector_heartbeat"]
    assert heartbeat["passed"] is False
    assert "sensibo collect --daemon" in heartbeat["remediation"]
    # A warning-severity check must not flip doctor's overall exit code.
    assert rc == 0


def test_doctor_reports_fresh_heartbeat_healthy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sensibo.db"
    with Store(db_path=db) as store:
        store.set_meta("last_cycle_at", str(time.time()))
        store.set_meta("last_cycle_outcome", "ok")

    rc = main(["doctor", "--db", str(db), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    checks = {c["id"]: c for c in payload["checks"]}
    heartbeat = checks["collector_heartbeat"]
    assert heartbeat["passed"] is True
    assert heartbeat["remediation"] == ""


def test_doctor_reports_stale_heartbeat_unhealthy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "sensibo.db"
    stale_at = time.time() - (4 * 90)  # more than 3x DEFAULT_INTERVAL (90s) ago
    with Store(db_path=db) as store:
        store.set_meta("last_cycle_at", str(stale_at))
        store.set_meta("last_cycle_outcome", "ok")

    main(["doctor", "--db", str(db), "--json"])
    payload = json.loads(capsys.readouterr().out)
    checks = {c["id"]: c for c in payload["checks"]}
    heartbeat = checks["collector_heartbeat"]
    assert heartbeat["passed"] is False


def test_doctor_judges_the_heartbeat_against_the_daemons_configured_interval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Qodo 14: a 10-minute cadence must not read as a dead collector.

    The same 1000s-old heartbeat is healthy when the daemon published
    ``collect_interval=600`` (3x600 = 1800s of slack) and unhealthy under the
    90s default (3x90 = 270s).
    """
    beat_at = time.time() - 1000

    slow = tmp_path / "slow.db"
    with Store(db_path=slow) as store:
        store.set_meta("last_cycle_at", str(beat_at))
        store.set_meta("last_cycle_outcome", "ok")
        store.set_meta("collect_interval", repr(600.0))
    main(["doctor", "--db", str(slow), "--json"])
    heartbeat = _heartbeat_check(capsys)
    assert heartbeat["passed"] is True
    assert "interval=600s" in heartbeat["message"]

    default = tmp_path / "default.db"
    with Store(db_path=default) as store:
        store.set_meta("last_cycle_at", str(beat_at))
        store.set_meta("last_cycle_outcome", "ok")
    main(["doctor", "--db", str(default), "--json"])
    heartbeat = _heartbeat_check(capsys)
    assert heartbeat["passed"] is False
    assert "interval=90s" in heartbeat["message"]


def _heartbeat_check(capsys: pytest.CaptureFixture[str]) -> dict:
    payload = json.loads(capsys.readouterr().out)
    return {c["id"]: c for c in payload["checks"]}["collector_heartbeat"]


def test_doctor_still_healthy_via_sensibo_db_env_with_no_heartbeat(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh store (no collector ever run) must not flip doctor's exit code.

    Exercises the ``SENSIBO_DB`` environment fallback (no ``--db`` flag) so
    this never touches the real ``~/.sensibo/sensibo.db``.
    """
    db = tmp_path / "sensibo.db"
    monkeypatch.setenv("SENSIBO_DB", str(db))

    rc = main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["healthy"] is True
    checks = {c["id"]: c for c in payload["checks"]}
    assert checks["collector_heartbeat"]["passed"] is False
