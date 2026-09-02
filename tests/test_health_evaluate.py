"""Tests for the pure health evaluation engine (task t2).

One acceptance criterion per section. Everything here is deterministic: the
engine takes ``now`` as a parameter, so no test reads a clock, touches the
store, or could make a network call.
"""

from __future__ import annotations

import ast
import datetime
import inspect
from pathlib import Path

import pytest

import sensibo.health
from sensibo.health import (
    EXECUTION_LOCAL,
    NOTIFY_COLLECTOR_RECOVERED,
    NOTIFY_COLLECTOR_UNHEALTHY,
    NOTIFY_DOWN,
    NOTIFY_RECOVERED,
    STATUS_DOWN,
    STATUS_OK,
    STATUS_UNKNOWN,
    STATUS_UNKNOWN_PARENT_DOWN,
    CollectorOutcome,
    EvaluationResult,
    HealthConfig,
    HealthState,
    Notification,
    Observation,
    Transition,
    evaluate,
    iso8601,
)

T0 = 1_756_800_000.0  # a fixed reference instant
OK = CollectorOutcome(ok=True, error=None)
FAILED = CollectorOutcome(ok=False, error="ApiError: cloud unreachable")

POD = "8DdxNuyc"
SENSOR = "ms_kDup7cVx"
SENSOR2 = "ms_o7dH4GeY"


def pod_obs(*, last: float | None, is_alive: bool | None = True, pod_id: str = POD) -> Observation:
    return Observation(
        location_id=pod_id, kind="pod", parent_pod_id=None, last_reading_at=last, is_alive=is_alive
    )


def sensor_obs(
    *,
    last: float | None,
    is_alive: bool | None = True,
    location_id: str = SENSOR,
    parent: str | None = POD,
) -> Observation:
    return Observation(
        location_id=location_id,
        kind="room_sensor",
        parent_pod_id=parent,
        last_reading_at=last,
        is_alive=is_alive,
    )


def kinds(result: EvaluationResult) -> list[str]:
    return [note.kind for note in result.notifications]


# --- criterion 1: the package, the shapes, and purity -----------------------


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_health_package_is_pure_stdlib() -> None:
    """No import of the store, the api client, the rules engine, or the cli."""
    package = Path(sensibo.health.__file__).parent
    for path in sorted(package.glob("*.py")):
        for name in _imported_modules(path):
            assert not name.startswith("sensibo.store"), path
            assert not name.startswith("sensibo.api"), path
            assert not name.startswith("sensibo.cli"), path
            assert not name.startswith("sensibo.rules"), path


def test_the_execution_marker_matches_the_rules_engine() -> None:
    """Health output declares local execution in exactly the words rules use."""
    from sensibo.rules import EXECUTION_LOCAL as RULES_MARKER

    assert EXECUTION_LOCAL == RULES_MARKER


def test_evaluate_signature_is_the_agreed_contract() -> None:
    params = list(inspect.signature(evaluate).parameters)
    assert params == [
        "previous",
        "observations",
        "collector",
        "now",
        "config",
        "collector_previous_ok",
    ]


def test_records_are_frozen_dataclasses() -> None:
    state = HealthState(location_id=POD, status=STATUS_OK, since=T0)
    with pytest.raises(Exception):
        state.status = STATUS_DOWN  # type: ignore[misc]


def test_notification_carries_the_local_execution_marker() -> None:
    result = evaluate({}, [pod_obs(last=T0 - 5000)], OK, T0, HealthConfig())
    assert EXECUTION_LOCAL == "local (stops when this daemon stops)"
    assert [note.execution for note in result.notifications] == [EXECUTION_LOCAL]


def test_config_defaults_and_from_env() -> None:
    config = HealthConfig()
    assert config.down_after_seconds == 900.0
    assert config.recovery_hold_cycles == 2
    assert config.cooldown_seconds == 3600.0
    assert config.daily_cap == 20

    overridden = HealthConfig.from_env(
        {
            "SENSIBO_HEALTH_DOWN_AFTER": "300",
            "SENSIBO_HEALTH_COOLDOWN": "60",
            "SENSIBO_HEALTH_DAILY_CAP": "3",
        }
    )
    assert (overridden.down_after_seconds, overridden.cooldown_seconds) == (300.0, 60.0)
    assert overridden.daily_cap == 3
    assert HealthConfig.from_env({}) == HealthConfig()


def test_config_rejects_nonsense_env_values() -> None:
    with pytest.raises(ValueError, match="must be a number"):
        HealthConfig.from_env({"SENSIBO_HEALTH_DOWN_AFTER": "soon"})
    with pytest.raises(ValueError, match="must not be negative"):
        HealthConfig.from_env({"SENSIBO_HEALTH_DOWN_AFTER": "-1"})
    with pytest.raises(ValueError, match="must not be negative"):
        HealthConfig(daily_cap=-2)


# --- Q13: nonfinite thresholds must not silently disable alerting -----------


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_from_env_rejects_nonfinite_down_after(raw: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        HealthConfig.from_env({"SENSIBO_HEALTH_DOWN_AFTER": raw})


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_from_env_rejects_nonfinite_cooldown(raw: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        HealthConfig.from_env({"SENSIBO_HEALTH_COOLDOWN": raw})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_health_config_rejects_nonfinite_down_after_seconds(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        HealthConfig(down_after_seconds=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_health_config_rejects_nonfinite_cooldown_seconds(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        HealthConfig(cooldown_seconds=value)


# --- criterion 2: threshold, isAlive, and the recovery hold -----------------


def test_a_location_past_the_threshold_goes_down() -> None:
    previous = {POD: HealthState(location_id=POD, status=STATUS_OK, since=T0 - 10_000)}
    result = evaluate(previous, [pod_obs(last=T0 - 901)], OK, T0, HealthConfig())

    assert result.states[POD].status == STATUS_DOWN
    assert result.states[POD].since == T0
    assert result.transitions == (
        Transition(location_id=POD, from_status=STATUS_OK, to_status=STATUS_DOWN, at=T0),
    )
    assert kinds(result) == [NOTIFY_DOWN]


def test_a_location_inside_the_threshold_stays_ok() -> None:
    previous = {POD: HealthState(location_id=POD, status=STATUS_OK, since=T0 - 10_000)}
    result = evaluate(previous, [pod_obs(last=T0 - 899)], OK, T0, HealthConfig())

    assert result.states[POD].status == STATUS_OK
    assert result.states[POD].since == T0 - 10_000  # unchanged, no transition
    assert result.transitions == ()
    assert result.notifications == ()


def test_is_alive_false_goes_down_even_with_a_fresh_reading() -> None:
    previous = {POD: HealthState(location_id=POD, status=STATUS_OK, since=T0 - 10_000)}
    result = evaluate(previous, [pod_obs(last=T0 - 30, is_alive=False)], OK, T0, HealthConfig())

    assert result.states[POD].status == STATUS_DOWN
    assert kinds(result) == [NOTIFY_DOWN]


def test_recovery_needs_two_consecutive_cycles() -> None:
    config = HealthConfig()
    states: dict[str, HealthState] = {}

    # cycle 0: down.
    result = evaluate(states, [pod_obs(last=T0 - 5000)], OK, T0, config)
    states = dict(result.states)
    assert states[POD].status == STATUS_DOWN

    # cycle 1: reporting again — but the hold is not satisfied yet.
    result = evaluate(states, [pod_obs(last=T0 + 90)], OK, T0 + 90, config)
    states = dict(result.states)
    assert states[POD].status == STATUS_DOWN, "one good cycle must not clear a down"
    assert states[POD].ok_streak == 1
    assert result.transitions == ()
    assert result.notifications == ()

    # cycle 2: the hold is satisfied — back to ok, exactly one recovery.
    result = evaluate(states, [pod_obs(last=T0 + 180)], OK, T0 + 180, config)
    states = dict(result.states)
    assert states[POD].status == STATUS_OK
    assert states[POD].since == T0 + 180
    assert states[POD].last_ok == T0 + 180
    assert result.transitions == (
        Transition(location_id=POD, from_status=STATUS_DOWN, to_status=STATUS_OK, at=T0 + 180),
    )
    assert kinds(result) == [NOTIFY_RECOVERED]

    # cycle 3: still ok — no further notification.
    result = evaluate(states, [pod_obs(last=T0 + 270)], OK, T0 + 270, config)
    assert result.notifications == ()
    assert result.transitions == ()


def test_a_longer_hold_is_honoured() -> None:
    config = HealthConfig(recovery_hold_cycles=4)
    states = {POD: HealthState(location_id=POD, status=STATUS_DOWN, since=T0, last_notified_at=T0)}
    for index in range(1, 4):
        result = evaluate(states, [pod_obs(last=T0 + 90 * index)], OK, T0 + 90 * index, config)
        states = dict(result.states)
        assert states[POD].status == STATUS_DOWN
    result = evaluate(states, [pod_obs(last=T0 + 360)], OK, T0 + 360, config)
    assert result.states[POD].status == STATUS_OK


# --- criterion 3: collector health is a distinct state ----------------------


def test_collector_failure_marks_everything_unknown_and_notifies_once() -> None:
    config = HealthConfig()
    previous = {
        POD: HealthState(location_id=POD, status=STATUS_OK, since=T0 - 10_000),
        SENSOR: HealthState(location_id=SENSOR, status=STATUS_OK, since=T0 - 10_000),
    }

    first = evaluate(previous, [], FAILED, T0, config, collector_previous_ok=True)
    assert first.collector_ok is False
    assert {state.status for state in first.states.values()} == {STATUS_UNKNOWN}
    assert kinds(first) == [NOTIFY_COLLECTOR_UNHEALTHY]
    assert all(t.to_status == STATUS_UNKNOWN for t in first.transitions)
    assert not any(t.to_status == STATUS_DOWN for t in first.transitions)
    assert "cloud unreachable" in first.notifications[0].message

    # a second failed cycle: still unknown, but no second notification.
    second = evaluate(dict(first.states), [], FAILED, T0 + 90, config, collector_previous_ok=False)
    assert second.notifications == ()
    assert second.transitions == ()
    assert {state.status for state in second.states.values()} == {STATUS_UNKNOWN}


def test_collector_recovery_notifies_once_and_locations_re_evaluate() -> None:
    config = HealthConfig()
    states = {
        POD: HealthState(location_id=POD, status=STATUS_UNKNOWN, since=T0),
        SENSOR: HealthState(location_id=SENSOR, status=STATUS_UNKNOWN, since=T0),
    }
    result = evaluate(
        states,
        [pod_obs(last=T0 + 90), sensor_obs(last=T0 - 100_000)],
        OK,
        T0 + 90,
        config,
        collector_previous_ok=False,
    )
    assert result.collector_ok is True
    assert kinds(result).count(NOTIFY_COLLECTOR_RECOVERED) == 1
    # the pod re-evaluates normally: one good cycle is not yet a recovery,
    # while the long-silent sensor is genuinely down.
    assert result.states[POD].status == STATUS_UNKNOWN
    assert result.states[SENSOR].status == STATUS_DOWN
    assert NOTIFY_DOWN in kinds(result)


def test_collector_failure_on_the_first_ever_cycle_notifies() -> None:
    result = evaluate(
        {}, [pod_obs(last=T0)], FAILED, T0, HealthConfig(), collector_previous_ok=None
    )
    assert kinds(result) == [NOTIFY_COLLECTOR_UNHEALTHY]
    assert result.states[POD].status == STATUS_UNKNOWN


# --- criterion 4: parent-down suppression ----------------------------------


def test_parent_pod_down_marks_children_unknown_parent_down() -> None:
    config = HealthConfig()
    observations = [
        pod_obs(last=T0 - 5000),
        sensor_obs(last=T0 - 5000, location_id=SENSOR),
        sensor_obs(last=T0 - 5000, location_id=SENSOR2),
    ]
    result = evaluate({}, observations, OK, T0, config)

    assert result.states[POD].status == STATUS_DOWN
    assert result.states[SENSOR].status == STATUS_UNKNOWN_PARENT_DOWN
    assert result.states[SENSOR2].status == STATUS_UNKNOWN_PARENT_DOWN
    assert kinds(result) == [NOTIFY_DOWN]
    assert result.notifications[0].location_id == POD
    assert POD in result.notifications[0].message
    assert not any(note.location_id in (SENSOR, SENSOR2) for note in result.notifications)


def test_a_child_going_quiet_while_the_parent_is_alive_is_a_real_down() -> None:
    result = evaluate(
        {},
        [pod_obs(last=T0 - 30), sensor_obs(last=T0 - 5000)],
        OK,
        T0,
        HealthConfig(),
    )
    assert result.states[POD].status == STATUS_OK
    assert result.states[SENSOR].status == STATUS_DOWN
    assert [note.location_id for note in result.notifications] == [SENSOR]


def test_an_unobserved_but_down_parent_still_suppresses_its_children() -> None:
    previous = {POD: HealthState(location_id=POD, status=STATUS_DOWN, since=T0 - 5000)}
    result = evaluate(previous, [sensor_obs(last=T0 - 5000)], OK, T0, HealthConfig())
    assert result.states[SENSOR].status == STATUS_UNKNOWN_PARENT_DOWN
    assert result.notifications == ()


def test_a_child_of_an_entirely_unknown_parent_is_judged_on_its_own() -> None:
    """No record of the parent at all: no suppression to justify, so evaluate."""
    result = evaluate({}, [sensor_obs(last=T0 - 5000, parent="never-seen")], OK, T0, HealthConfig())
    assert result.states[SENSOR].status == STATUS_DOWN
    assert kinds(result) == [NOTIFY_DOWN]


# --- criterion 5: cooldown and the daily cap -------------------------------


def _flap(config: HealthConfig, cycles: int, *, period: int = 90) -> tuple[list[str], int]:
    """Drive a sensor that alternates silent/reporting on a 2-cycle rhythm."""
    states: dict[str, HealthState] = {}
    seen: list[str] = []
    suppressed = 0
    for index in range(cycles):
        now = T0 + period * index
        silent = (index // 2) % 2 == 0
        last = now - 100_000 if silent else now
        result = evaluate(states, [pod_obs(last=last)], OK, now, config)
        states = dict(result.states)
        seen.extend(kinds(result))
        suppressed += result.suppressed_count
    return seen, suppressed


def test_a_flapping_sensor_is_capped_at_one_pair_per_cooldown_window() -> None:
    config = HealthConfig(cooldown_seconds=3600.0)
    seen, suppressed = _flap(config, cycles=20)  # 20 * 90s = 30 minutes < cooldown
    assert seen.count(NOTIFY_DOWN) == 1
    assert seen.count(NOTIFY_RECOVERED) == 1
    assert suppressed >= 1


def test_a_sensor_that_never_holds_two_good_cycles_never_recovers() -> None:
    config = HealthConfig(cooldown_seconds=3600.0)
    states: dict[str, HealthState] = {}
    seen: list[str] = []
    for index in range(12):
        now = T0 + 90 * index
        last = now - 100_000 if index % 2 == 0 else now
        result = evaluate(states, [pod_obs(last=last)], OK, now, config)
        states = dict(result.states)
        seen.extend(kinds(result))
    assert seen == [NOTIFY_DOWN]
    assert states[POD].status == STATUS_DOWN


def test_the_daily_cap_suppresses_further_notifications_and_counts_them() -> None:
    config = HealthConfig(cooldown_seconds=0.0, daily_cap=2)
    seen, suppressed = _flap(config, cycles=24)
    assert len(seen) == 2, "the daily cap is the ceiling for one location"
    assert suppressed > 0


def test_the_daily_counter_resets_on_a_new_utc_day() -> None:
    config = HealthConfig(cooldown_seconds=0.0, daily_cap=1)
    states = {
        POD: HealthState(
            location_id=POD,
            status=STATUS_OK,
            since=T0,
            last_notified_at=T0,
            notifications_today=1,
            day_key=iso8601(T0)[:10],
        )
    }
    same_day = evaluate(states, [pod_obs(last=T0 - 5000)], OK, T0 + 60, config)
    assert same_day.notifications == ()
    assert same_day.suppressed_count == 1

    tomorrow = T0 + 86_400 * 2
    next_day = evaluate(states, [pod_obs(last=tomorrow - 5000)], OK, tomorrow, config)
    assert kinds(next_day) == [NOTIFY_DOWN]
    assert next_day.states[POD].notifications_today == 1
    assert next_day.states[POD].day_key == iso8601(tomorrow)[:10]


# --- criterion 6: first-run seeding ----------------------------------------


def test_first_run_seeds_every_location_and_alerts_only_the_stale_ones() -> None:
    observations = [
        pod_obs(last=T0 - 30),
        sensor_obs(last=T0 - 100_000, location_id=SENSOR, parent=None),
        sensor_obs(last=T0 - 60, location_id=SENSOR2, parent=None),
    ]
    result = evaluate({}, observations, OK, T0, HealthConfig())

    assert set(result.states) == {POD, SENSOR, SENSOR2}
    assert result.states[POD].status == STATUS_OK
    assert result.states[SENSOR2].status == STATUS_OK
    assert result.states[SENSOR].status == STATUS_DOWN
    assert [note.location_id for note in result.notifications] == [SENSOR]
    assert [t.from_status for t in result.transitions] == [None, None, None]


def test_first_run_with_a_location_that_never_reported_is_down() -> None:
    result = evaluate({}, [pod_obs(last=None)], OK, T0, HealthConfig())
    assert result.states[POD].status == STATUS_DOWN
    assert "never" in result.notifications[0].message


# --- carry-forward and message shape ---------------------------------------


def test_a_location_absent_from_observations_keeps_its_state() -> None:
    previous = {
        POD: HealthState(location_id=POD, status=STATUS_DOWN, since=T0 - 5000, last_ok=T0 - 9000)
    }
    result = evaluate(previous, [sensor_obs(last=T0, parent=None)], OK, T0, HealthConfig())
    assert result.states[POD] == previous[POD]
    assert POD not in {t.location_id for t in result.transitions}


def test_notification_messages_name_the_location_and_the_iso_last_heard_time() -> None:
    last = T0 - 5000
    result = evaluate({}, [pod_obs(last=last)], OK, T0, HealthConfig())
    note = result.notifications[0]
    assert isinstance(note, Notification)
    assert note.location_id == POD
    assert note.at == T0
    assert POD in note.message
    stamp = iso8601(last)
    assert stamp in note.message
    assert stamp.endswith("Z")
    assert datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is not None


def test_iso8601_renders_utc_regardless_of_the_host_timezone() -> None:
    assert iso8601(0.0) == "1970-01-01T00:00:00Z"
    assert iso8601(None) == "never"


# --- review fixes: recoveries are never lost --------------------------------


def test_recovery_is_sent_even_when_the_daily_cap_is_exhausted() -> None:
    config = HealthConfig(daily_cap=1, cooldown_seconds=0.0)
    r1 = evaluate({}, [pod_obs(last=T0 - 100_000)], OK, T0, config)
    assert kinds(r1) == [NOTIFY_DOWN]
    states = dict(r1.states)
    for step in (1, 2):
        result = evaluate(states, [pod_obs(last=T0 + 90 * step)], OK, T0 + 90 * step, config)
        states = dict(result.states)
    assert kinds(result) == [NOTIFY_RECOVERED]
    assert states[POD].announced_down_since is None


def test_recovery_survives_a_collector_outage_between_down_and_up() -> None:
    config = HealthConfig(cooldown_seconds=0.0)
    r1 = evaluate({}, [pod_obs(last=T0 - 100_000)], OK, T0, config)
    assert kinds(r1) == [NOTIFY_DOWN]
    states = dict(r1.states)
    # The collector itself fails for two cycles: the location goes unknown.
    failed = CollectorOutcome(ok=False, error="boom")
    for step in (1, 2):
        result = evaluate(states, [], failed, T0 + 90 * step, config, collector_previous_ok=True)
        states = dict(result.states)
    assert states[POD].status == STATUS_UNKNOWN
    assert states[POD].announced_down_since is not None
    # The collector comes back and the sensor is reporting again.
    seen: list[str] = []
    for step in (3, 4, 5):
        result = evaluate(
            states,
            [pod_obs(last=T0 + 90 * step)],
            OK,
            T0 + 90 * step,
            config,
            collector_previous_ok=False if step == 3 else True,
        )
        states = dict(result.states)
        seen.extend(kinds(result))
    assert NOTIFY_RECOVERED in seen
    assert states[POD].status == STATUS_OK
