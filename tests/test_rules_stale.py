"""The ``stale`` rule leaf: reacting to a sensor that stopped reporting (task t8).

A rule may not trust a dead room. These tests cover the leaf's validation, its
evaluation against the health rows the collector persists, and the dry-run
explanation an operator reads before arming — plus the example rule shipped in
``examples/``.

Nothing here touches the compressor-safety code (the minimum off-time /
hysteresis gate, the rate limiter): this leaf only adds a *condition*, and
:mod:`sensibo.rules.engine` and :mod:`sensibo.rules.persistence` are unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sensibo.health.model import (
    STATUS_DOWN,
    STATUS_OK,
    STATUS_UNKNOWN,
    STATUS_UNKNOWN_PARENT_DOWN,
    iso8601,
)
from sensibo.rules.evaluate import evaluate
from sensibo.rules.model import LEAF_TYPES, Rule, RuleValidationError
from sensibo.store import Store

_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "stale-room.rule.json"


def _rule(condition: dict) -> dict:
    return {
        "name": "stale-guard",
        "pod": "ac1",
        "action": {"on": False},
        "conditions": condition,
    }


def _seed(db: Path, location_id: str = "sensor1") -> None:
    with Store(db_path=db) as store:
        store.upsert_location("ac1", kind="pod", product_model="airq")
        store.upsert_location(
            location_id,
            kind="room_sensor",
            parent_pod_id="ac1",
            room_name="Bedroom",
        )


def _set_health(db: Path, location_id: str, status: str, last_ok: float | None) -> None:
    with Store(db_path=db) as store:
        store.set_health(location_id, status=status, since=1000.0, last_ok=last_ok)


# --- criterion 1: the schema -------------------------------------------------


def test_stale_is_a_leaf_type() -> None:
    assert "stale" in LEAF_TYPES


def test_minimal_stale_leaf_validates() -> None:
    rule = Rule.from_dict(_rule({"type": "stale", "location": "Bedroom"}))
    assert rule.conditions["type"] == "stale"


def test_stale_leaf_accepts_a_positive_after_seconds() -> None:
    rule = Rule.from_dict(_rule({"type": "stale", "location": "Bedroom", "after_seconds": 900}))
    assert rule.conditions["after_seconds"] == 900


def test_stale_leaf_requires_a_location() -> None:
    no_location = _rule({"type": "stale"})
    with pytest.raises(RuleValidationError):
        Rule.from_dict(no_location)

    blank_location = _rule({"type": "stale", "location": "   "})
    with pytest.raises(RuleValidationError):
        Rule.from_dict(blank_location)


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "900", None])
def test_stale_after_seconds_must_be_a_positive_int(bad: object) -> None:
    payload = _rule({"type": "stale", "location": "Bedroom", "after_seconds": bad})
    with pytest.raises(RuleValidationError):
        Rule.from_dict(payload)


def test_stale_leaf_rejects_unknown_keys() -> None:
    payload = _rule({"type": "stale", "location": "Bedroom", "field": "motion"})
    with pytest.raises(RuleValidationError) as excinfo:
        Rule.from_dict(payload)
    assert "field" in str(excinfo.value)


# --- criterion 2: evaluation against the persisted health row ----------------


@pytest.mark.parametrize("status", [STATUS_DOWN, STATUS_UNKNOWN, STATUS_UNKNOWN_PARENT_DOWN])
def test_a_not_ok_status_is_stale(tmp_path: Path, status: str) -> None:
    db = tmp_path / "sensibo.db"
    _seed(db)
    _set_health(db, "sensor1", status, last_ok=5000.0)
    with Store(db_path=db) as store:
        result = evaluate(store, {"type": "stale", "location": "Bedroom"}, now_ts=5100.0)
    assert result.met is True


def test_an_ok_status_is_not_stale(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    _seed(db)
    _set_health(db, "sensor1", STATUS_OK, last_ok=5000.0)
    with Store(db_path=db) as store:
        result = evaluate(store, {"type": "stale", "location": "Bedroom"}, now_ts=5100.0)
    assert result.met is False


def test_a_location_with_no_health_row_is_stale(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    _seed(db)
    with Store(db_path=db) as store:
        result = evaluate(store, {"type": "stale", "location": "Bedroom"}, now_ts=5100.0)
    assert result.met is True
    assert STATUS_UNKNOWN in result.detail


def test_after_seconds_makes_an_ok_but_old_row_stale(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    _seed(db)
    _set_health(db, "sensor1", STATUS_OK, last_ok=5000.0)
    cond = {"type": "stale", "location": "Bedroom", "after_seconds": 600}
    with Store(db_path=db) as store:
        fresh = evaluate(store, cond, now_ts=5300.0)
        old = evaluate(store, cond, now_ts=6000.0)
    assert fresh.met is False
    assert old.met is True


def test_after_seconds_with_no_last_ok_is_stale(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    _seed(db)
    _set_health(db, "sensor1", STATUS_OK, last_ok=None)
    with Store(db_path=db) as store:
        result = evaluate(
            store,
            {"type": "stale", "location": "Bedroom", "after_seconds": 600},
            now_ts=5300.0,
        )
    assert result.met is True


def test_an_unresolvable_location_is_unmet_not_an_exception(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    _seed(db)
    with Store(db_path=db) as store:
        result = evaluate(store, {"type": "stale", "location": "Attic"}, now_ts=5100.0)
    assert result.met is False
    assert "Attic" in result.detail


def test_a_location_driven_reporting_then_silent_then_reporting(tmp_path: Path) -> None:
    """The whole life-cycle: healthy -> the collector marks it down -> recovered."""
    db = tmp_path / "sensibo.db"
    _seed(db)
    cond = {"type": "stale", "location": "Bedroom"}

    _set_health(db, "sensor1", STATUS_OK, last_ok=5000.0)
    with Store(db_path=db) as store:
        assert evaluate(store, cond, now_ts=5010.0).met is False

    _set_health(db, "sensor1", STATUS_DOWN, last_ok=5000.0)
    with Store(db_path=db) as store:
        assert evaluate(store, cond, now_ts=9000.0).met is True

    _set_health(db, "sensor1", STATUS_OK, last_ok=9500.0)
    with Store(db_path=db) as store:
        assert evaluate(store, cond, now_ts=9510.0).met is False


# --- criterion 3: the dry-run explanation ------------------------------------


def test_the_explanation_names_the_status_and_the_last_ok_timestamp(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    _seed(db)
    _set_health(db, "sensor1", STATUS_DOWN, last_ok=5000.0)
    with Store(db_path=db) as store:
        result = evaluate(store, {"type": "stale", "location": "Bedroom"}, now_ts=9000.0)
    rendered = "\n".join(result.render_lines())
    assert "Bedroom" in rendered
    assert STATUS_DOWN in rendered
    assert iso8601(5000.0) in rendered
    assert result.to_dict()["met"] is True


def test_the_explanation_says_never_when_a_location_was_never_ok(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    _seed(db)
    _set_health(db, "sensor1", STATUS_DOWN, last_ok=None)
    with Store(db_path=db) as store:
        result = evaluate(store, {"type": "stale", "location": "Bedroom"}, now_ts=9000.0)
    assert "never" in result.detail


def test_the_explanation_reports_the_after_seconds_budget(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    _seed(db)
    _set_health(db, "sensor1", STATUS_OK, last_ok=5000.0)
    with Store(db_path=db) as store:
        result = evaluate(
            store,
            {"type": "stale", "location": "Bedroom", "after_seconds": 600},
            now_ts=5300.0,
        )
    assert "600" in result.detail


# --- criterion 5: the shipped example ----------------------------------------


def test_the_stale_room_example_validates() -> None:
    data = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    rule = Rule.from_dict(data)
    assert rule.action.get("on") is False
    assert data["comment"].strip()


def test_the_stale_room_example_fires_when_the_sensor_is_dead(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    _seed(db)
    _set_health(db, "sensor1", STATUS_DOWN, last_ok=5000.0)
    rule = Rule.from_dict(json.loads(_EXAMPLE.read_text(encoding="utf-8")))
    with Store(db_path=db) as store:
        result = evaluate(store, rule.conditions, now_ts=9000.0)
    assert result.met is True
