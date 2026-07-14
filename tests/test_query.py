"""Tests for ``sensibo query`` — offline reads from the local store (task t7).

Written first (TDD): these fail against a CLI with no ``query`` verb and pass
once ``sensibo/cli/_commands/query.py`` lands.

Hard rule enforced throughout: ``query`` must **never** touch the network — it
answers from SQLite alone. Every test seeds a ``tmp_path`` store (never the
real ``~/.sensibo``) via the :class:`~sensibo.store.Store` API, then drives the
CLI through :func:`sensibo.cli.main`.
"""

from __future__ import annotations

import datetime
import json
import socket
from pathlib import Path

import pytest

from sensibo.cli import main
from sensibo.store import Store

# --- fixtures ----------------------------------------------------------------


def _seed_two_locations(db_path: Path) -> None:
    """A pod with two fields at two instants, plus a Room Sensor under it."""
    with Store(db_path=db_path) as store:
        store.upsert_location(
            "pod-1", kind="pod", product_model="elements", room_name="Living Room"
        )
        store.record_reading("pod-1", "temperature", 20.0, timestamp=100.0)
        store.record_reading("pod-1", "temperature", 21.0, timestamp=200.0)
        store.record_reading("pod-1", "temperature", 22.0, timestamp=300.0)
        store.record_reading("pod-1", "humidity", 55.0, timestamp=300.0)
        store.upsert_location(
            "ms_abc",
            kind="room_sensor",
            parent_pod_id="pod-1",
            room_name="Living Room",
            seen_at=300.0,
        )
        store.record_reading("ms_abc", "temperature", 19.5, timestamp=300.0)


def _iso(ts: float) -> str:
    return (
        datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


@pytest.fixture()
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blow up on any socket use — proves `query` never touches the network."""

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise OSError("network disabled for this test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


# --- query latest --------------------------------------------------------


def test_latest_all_locations_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)

    rc = main(["query", "latest", "--db", str(db), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    readings = payload["readings"]
    by_key = {(r["location_id"], r["field"]): r for r in readings}
    assert by_key[("pod-1", "temperature")]["value"] == 22.0
    assert by_key[("pod-1", "temperature")]["timestamp"] == 300.0
    assert by_key[("pod-1", "humidity")]["value"] == 55.0
    assert by_key[("ms_abc", "temperature")]["value"] == 19.5


def test_latest_specific_location_all_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)

    rc = main(["query", "latest", "pod-1", "--db", str(db), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    fields = {r["field"] for r in payload["readings"]}
    assert fields == {"temperature", "humidity"}
    assert all(r["location_id"] == "pod-1" for r in payload["readings"])


def test_latest_location_and_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)

    rc = main(["query", "latest", "pod-1", "--field", "temperature", "--db", str(db), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["readings"]) == 1
    reading = payload["readings"][0]
    assert reading["location_id"] == "pod-1"
    assert reading["field"] == "temperature"
    assert reading["value"] == 22.0
    assert reading["unit"] == "C"


def test_latest_text_output_is_nonempty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)

    rc = main(["query", "latest", "pod-1", "--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "pod-1" in out
    assert "temperature" in out


def test_latest_unknown_location_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)

    rc = main(["query", "latest", "nonexistent-pod", "--db", str(db)])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "sensibo collect" in err


def test_latest_empty_store_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "empty.db"
    # Touch the store so the file exists, but seed nothing.
    Store(db_path=db).close()

    rc = main(["query", "latest", "--db", str(db)])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "sensibo collect" in err


# --- query range -----------------------------------------------------------


def test_range_no_bounds_returns_everything_in_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)

    rc = main(["query", "range", "pod-1", "--field", "temperature", "--db", str(db), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    timestamps = [r["timestamp"] for r in payload["readings"]]
    assert timestamps == [100.0, 200.0, 300.0]


def test_range_since_and_until_are_inclusive_boundaries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)

    rc = main(
        [
            "query",
            "range",
            "pod-1",
            "--field",
            "temperature",
            "--since",
            _iso(100.0),
            "--until",
            _iso(200.0),
            "--db",
            str(db),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    timestamps = [r["timestamp"] for r in payload["readings"]]
    # Both boundary timestamps are included (inclusive on both ends), the
    # third reading (300.0) is excluded.
    assert timestamps == [100.0, 200.0]


def test_range_since_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)

    rc = main(
        [
            "query",
            "range",
            "pod-1",
            "--field",
            "temperature",
            "--since",
            _iso(200.0),
            "--db",
            str(db),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    timestamps = [r["timestamp"] for r in payload["readings"]]
    assert timestamps == [200.0, 300.0]


def test_range_until_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)

    rc = main(
        [
            "query",
            "range",
            "pod-1",
            "--field",
            "temperature",
            "--until",
            _iso(200.0),
            "--db",
            str(db),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    timestamps = [r["timestamp"] for r in payload["readings"]]
    assert timestamps == [100.0, 200.0]


def test_range_unknown_location_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)

    rc = main(["query", "range", "nonexistent-pod", "--field", "temperature", "--db", str(db)])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "sensibo collect" in err


def test_range_invalid_since_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)

    rc = main(
        [
            "query",
            "range",
            "pod-1",
            "--field",
            "temperature",
            "--since",
            "not-a-date",
            "--db",
            str(db),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "ISO" in err


def test_range_requires_field_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)
    args = ["query", "range", "pod-1", "--db", str(db)]

    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- query locations --------------------------------------------------------


def test_locations_json_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)

    rc = main(["query", "locations", "--db", str(db), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    locations = {loc["id"]: loc for loc in payload["locations"]}
    assert set(locations) == {"pod-1", "ms_abc"}
    pod = locations["pod-1"]
    assert pod["kind"] == "pod"
    assert pod["product_model"] == "elements"
    assert pod["room_name"] == "Living Room"
    assert pod["alias"] is None
    assert "last_seen" in pod
    sensor = locations["ms_abc"]
    assert sensor["kind"] == "room_sensor"
    assert sensor["parent_pod_id"] == "pod-1"


def test_locations_reflects_alias(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)
    with Store(db_path=db) as store:
        store.set_alias("pod-1", "Downstairs")

    rc = main(["query", "locations", "--db", str(db), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    locations = {loc["id"]: loc for loc in payload["locations"]}
    assert locations["pod-1"]["alias"] == "Downstairs"


def test_locations_text_output_is_nonempty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "sensibo.db"
    _seed_two_locations(db)

    rc = main(["query", "locations", "--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "pod-1" in out
    assert "ms_abc" in out


def test_locations_empty_store_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    db = tmp_path / "empty.db"
    Store(db_path=db).close()

    rc = main(["query", "locations", "--db", str(db)])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "sensibo collect" in err


# --- --db / SENSIBO_DB precedence ------------------------------------------


def test_query_honors_sensibo_db_env_var(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    block_network: None,
) -> None:
    db = tmp_path / "env-store.db"
    _seed_two_locations(db)
    monkeypatch.setenv("SENSIBO_DB", str(db))

    rc = main(["query", "locations", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert {loc["id"] for loc in payload["locations"]} == {"pod-1", "ms_abc"}


def test_explicit_db_flag_wins_over_env_var(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    block_network: None,
) -> None:
    env_db = tmp_path / "env.db"
    explicit_db = tmp_path / "explicit.db"
    _seed_two_locations(explicit_db)
    Store(db_path=env_db).close()  # exists but empty
    monkeypatch.setenv("SENSIBO_DB", str(env_db))

    rc = main(["query", "locations", "--db", str(explicit_db), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert {loc["id"] for loc in payload["locations"]} == {"pod-1", "ms_abc"}


# --- registration / help / explain ------------------------------------------


def test_query_bare_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["query"])
    assert rc == 0
    assert capsys.readouterr().out.strip()


def test_query_help_lists_subverbs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["query", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "latest" in out
    assert "range" in out
    assert "locations" in out


def test_explain_query_resolves(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "query"])
    assert rc == 0
    assert "sensibo query" in capsys.readouterr().out
