"""CLI tests for ``sensibo room`` — the room naming registry (task t14).

Written first (TDD): these fail until ``sensibo/cli/_commands/room.py`` is
registered. Every test points ``SENSIBO_DB`` at a ``tmp_path`` file — the real
``~/.sensibo`` is never touched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from sensibo.cli import main
from sensibo.explain import known_paths
from sensibo.store import KIND_POD, KIND_ROOM_SENSOR, Store


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sensibo.db"
    monkeypatch.setenv("SENSIBO_DB", str(path))
    return path


def _seed(db_path: Path) -> None:
    """One airq main unit plus two Room Sensors — one of them stale."""
    now = time.time()
    five_months_ago = now - (150 * 86400)
    with Store(db_path=db_path) as store:
        store.upsert_location(
            "pod-airq",
            kind=KIND_POD,
            product_model="airq",
            room_name="Living Room",
            seen_at=now,
        )
        store.upsert_location(
            "ms_fresh",
            kind=KIND_ROOM_SENSOR,
            parent_pod_id="pod-airq",
            room_name="Bedroom",
            seen_at=now,
        )
        store.upsert_location(
            "ms_stale",
            kind=KIND_ROOM_SENSOR,
            parent_pod_id="pod-airq",
            room_name="Garage",
            seen_at=five_months_ago,
        )


# --- empty store: CliError with the "run collect first" remediation ----------


def test_room_list_on_empty_store_errors_with_remediation(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["room", "list"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "sensibo collect" in err


def test_room_list_on_empty_store_errors_with_remediation_json(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["room", "list", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert "sensibo collect" in payload["remediation"]


def test_room_name_on_empty_store_errors_with_remediation(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["room", "name", "anything", "New Name"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "sensibo collect" in err


# --- room list: shape, kinds, staleness ---------------------------------------


def test_room_list_json_shape(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(db_path)
    rc = main(["room", "list", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    ids = {loc["id"] for loc in payload["locations"]}
    assert ids == {"pod-airq", "ms_fresh", "ms_stale"}

    by_id = {loc["id"]: loc for loc in payload["locations"]}
    assert by_id["pod-airq"]["kind"] == "pod"
    assert by_id["ms_fresh"]["kind"] == "room_sensor"
    assert by_id["pod-airq"]["model"] == "airq"
    assert by_id["pod-airq"]["room_name"] == "Living Room"
    assert by_id["pod-airq"]["alias"] is None


def test_room_list_flags_the_stale_sensor(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(db_path)
    rc = main(["room", "list", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    by_id = {loc["id"]: loc for loc in payload["locations"]}
    assert by_id["ms_stale"]["stale"] is True
    assert by_id["ms_fresh"]["stale"] is False
    assert by_id["pod-airq"]["stale"] is False


def test_room_list_text_shows_stale_flag(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(db_path)
    rc = main(["room", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ms_stale" in out
    assert "STALE" in out
    # the fresh sensor's line must not carry the flag
    fresh_line = next(line for line in out.splitlines() if "ms_fresh" in line)
    assert "STALE" not in fresh_line


def test_room_list_stale_after_flag_overrides_threshold(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(db_path)
    # ms_fresh was seen "just now"; an absurdly small threshold makes even
    # that stale, proving --stale-after is honoured rather than hardcoded.
    rc = main(["room", "list", "--json", "--stale-after", "0"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    by_id = {loc["id"]: loc for loc in payload["locations"]}
    assert by_id["ms_fresh"]["stale"] is True
    assert payload["stale_after_hours"] == 0


# --- room name: dry-run by default, --apply persists --------------------------


def test_room_name_dry_run_does_not_persist(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(db_path)
    rc = main(["room", "name", "pod-airq", "Home Office"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would rename" in out
    assert "--apply" in out

    with Store(db_path=db_path) as store:
        assert store.get_location("pod-airq").alias is None


def test_room_name_apply_persists_via_set_alias(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(db_path)
    rc = main(["room", "name", "pod-airq", "Home Office", "--apply"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "renamed" in out

    with Store(db_path=db_path) as store:
        assert store.get_location("pod-airq").alias == "Home Office"


def test_room_name_by_current_sensibo_room_name(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(db_path)
    rc = main(["room", "name", "Living Room", "Home Office", "--apply"])
    assert rc == 0
    with Store(db_path=db_path) as store:
        assert store.get_location("pod-airq").alias == "Home Office"


def test_room_name_json_shape_dry_run(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(db_path)
    rc = main(["room", "name", "pod-airq", "Home Office", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "id": "pod-airq",
        "previous_name": "Living Room",
        "new_alias": "Home Office",
        "applied": False,
    }


def test_room_name_json_shape_applied(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(db_path)
    rc = main(["room", "name", "pod-airq", "Home Office", "--json", "--apply"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is True


# --- unknown / ambiguous names ------------------------------------------------


def test_room_name_unknown_location_errors_with_hint(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(db_path)
    rc = main(["room", "name", "no-such-location", "New Name"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


def test_room_name_ambiguous_location_errors_listing_candidates(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with Store(db_path=db_path) as store:
        store.upsert_location("pod-x", kind=KIND_POD, product_model="airq", room_name="Bedroom")
        store.upsert_location(
            "ms_y", kind=KIND_ROOM_SENSOR, parent_pod_id="pod-x", room_name="Bedroom"
        )
    rc = main(["room", "name", "Bedroom", "New Name"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "pod-x" in err
    assert "ms_y" in err


# --- rename-then-query continuity, exercised through the CLI -----------------


def test_cli_rename_then_query_reaches_the_same_history(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with Store(db_path=db_path) as store:
        store.upsert_location("pod-z", kind=KIND_POD, product_model="airq", room_name="Office")
        store.record_reading("pod-z", "temperature", 19.5, timestamp=500.0)

    rc = main(["room", "name", "pod-z", "Nursery", "--apply"])
    assert rc == 0
    capsys.readouterr()

    from sensibo.store.rooms import resolve_location

    with Store(db_path=db_path) as store:
        loc = resolve_location(store, "Nursery")
        rows = store.query_range(loc.id, "temperature")
        assert [r.value for r in rows] == [19.5]


# --- room overview / bare noun -------------------------------------------------


def test_room_bare_noun_prints_overview(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["room"])
    assert rc == 0
    assert capsys.readouterr().out.strip()


def test_room_overview_json_shape(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["room", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "sensibo room"
    assert isinstance(payload["sections"], list)
    assert payload["sections"]


# --- naming / usage: the console command is `sensibo`, never `sensibo-cli` --


def test_room_help_names_the_installed_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["room", "--help"])
    out = capsys.readouterr().out
    assert "sensibo room" in out
    assert "sensibo-cli room" not in out


def test_room_list_bad_flag_structured_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["room", "list", "--bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- explain catalog entries --------------------------------------------------


def test_room_paths_are_in_the_explain_catalog() -> None:
    paths = known_paths()
    assert ("room",) in paths
    assert ("room", "list") in paths
    assert ("room", "name") in paths


def test_explain_room_paths_resolve(capsys: pytest.CaptureFixture[str]) -> None:
    for path in (("room",), ("room", "list"), ("room", "name")):
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        capsys.readouterr()
