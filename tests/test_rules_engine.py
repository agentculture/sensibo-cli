"""Engine-level tests for the local rules engine (task t9).

These exercise :mod:`sensibo.rules` directly — the model, evaluation,
persistence, and the safety-critical engine — with a mocked AC client and
``tmp_path`` stores throughout. No test here makes, or could make, a real
network call or touch a real ``~/.sensibo`` file.

The load-bearing safety test is
:func:`test_flapping_condition_cannot_short_cycle_the_compressor`: it proves a
condition that oscillates every evaluation cannot cycle the compressor faster
than the configured minimum off-time.
"""

from __future__ import annotations

import datetime
import inspect
from pathlib import Path
from typing import Any

import pytest

from sensibo.rules import (
    DEFAULT_MIN_OFF_TIME_SECONDS,
    MIN_OFF_TIME_FLOOR_SECONDS,
    Rule,
    RulesStore,
    RuleValidationError,
    StoredRule,
    dry_run,
    effective_min_off_time,
    evaluate,
    run_once,
)
from sensibo.store import Store

# --- a mock AC client (records calls; mutates acState in place) --------------


class _FakeClient:
    def __init__(self, pods: dict[str, dict[str, Any]]) -> None:
        self._pods = {pid: dict(state) for pid, state in pods.items()}
        self.calls: list[tuple[str, tuple, dict]] = []

    def get_pod(self, pod_id: str, fields: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_pod", (pod_id,), {"fields": fields}))
        return {"result": {"acState": dict(self._pods[pod_id])}}

    def patch_ac_state(
        self, pod_id: str, prop: str, current_ac_state: dict[str, Any], new_value: object
    ) -> dict[str, Any]:
        self.calls.append(("patch_ac_state", (pod_id, prop, new_value), {}))
        self._pods[pod_id][prop] = new_value
        return {"result": dict(self._pods[pod_id])}

    def post_ac_states(self, pod_id: str, ac_state: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("post_ac_states", (pod_id, dict(ac_state)), {}))
        self._pods[pod_id].update(ac_state)
        return {"result": dict(self._pods[pod_id])}


# --- helpers -----------------------------------------------------------------


def _threshold_rule(name: str, pod: str, on: bool, op: str, value: float) -> Rule:
    action: dict[str, Any] = {"on": on}
    if on:
        action["mode"] = "cool"
    return Rule.from_dict(
        {
            "name": name,
            "pod": pod,
            "action": action,
            "conditions": {
                "all": [
                    {
                        "type": "threshold",
                        "location": pod,
                        "field": "temperature",
                        "op": op,
                        "value": value,
                    }
                ]
            },
        }
    )


def _arm(rules: RulesStore, name: str) -> None:
    rules.record_dry_run(name)
    rules.arm(name)


def _seed_pod(db: Path, pod_id: str = "ac1") -> None:
    with Store(db_path=db) as store:
        store.upsert_location(pod_id, kind="pod", product_model="airq")


# --- THE safety test: a flapping condition cannot short-cycle -----------------


def test_flapping_condition_cannot_short_cycle_the_compressor(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    rules_path = tmp_path / "rules.json"
    _seed_pod(db)

    rules = RulesStore(rules_path)
    rules.add(_threshold_rule("cool-on", "ac1", on=True, op=">", value=24))
    rules.add(_threshold_rule("cool-off", "ac1", on=False, op="<", value=22))
    _arm(rules, "cool-on")
    _arm(rules, "cool-off")

    client = _FakeClient({"ac1": {"on": False, "mode": "cool", "targetTemperature": 20}})
    min_off = 600.0
    step = 60.0
    start = 1000.0
    power_write_times: list[float] = []

    now = start
    for i in range(40):
        temp = 26.0 if i % 2 == 0 else 20.0  # oscillate hot/cold every pass
        # No clock jump here: advance BOTH the wall and the monotonic clock in
        # lockstep, so this exercises the gate's normal path (both clocks agree).
        with Store(db_path=db) as store:
            store.record_reading("ac1", "temperature", temp, timestamp=now)
            outcomes = run_once(store, rules, client, now_ts=now, mono_ts=now, min_off_time=min_off)
        for outcome in outcomes:
            if outcome.wrote and "on" in outcome.changes:
                power_write_times.append(now)
        now += step

    elapsed = 40 * step
    # It must have acted at least once...
    assert power_write_times, "the engine never drove the pod at all"
    # ...but far fewer times than the 40 flips a naive engine would perform...
    assert len(power_write_times) <= (elapsed / min_off) + 1
    # ...and every consecutive power change is at least min_off apart.
    gaps = [b - a for a, b in zip(power_write_times, power_write_times[1:])]
    assert all(gap >= min_off for gap in gaps), f"short-cycle detected: gaps={gaps}"


def test_hysteresis_state_survives_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    rules_path = tmp_path / "rules.json"
    _seed_pod(db)

    rules = RulesStore(rules_path)
    rules.add(_threshold_rule("cool-on", "ac1", on=True, op=">", value=24))
    rules.add(_threshold_rule("cool-off", "ac1", on=False, op="<", value=22))
    _arm(rules, "cool-on")
    _arm(rules, "cool-off")

    client = _FakeClient({"ac1": {"on": False, "mode": "cool", "targetTemperature": 20}})

    with Store(db_path=db) as store:
        store.record_reading("ac1", "temperature", 26.0, timestamp=1000.0)
        run_once(store, rules, client, now_ts=1000.0, min_off_time=600.0)
    assert client._pods["ac1"]["on"] is True

    # Simulate a full process restart: a brand-new store from the same file.
    rebooted = RulesStore(rules_path)
    assert rebooted.pod_state("ac1").last_power_change == 1000.0

    # A power flip 100s later must be refused from the *persisted* timestamp.
    with Store(db_path=db) as store:
        store.record_reading("ac1", "temperature", 20.0, timestamp=1100.0)
        outcomes = run_once(store, rebooted, client, now_ts=1100.0, min_off_time=600.0)
    assert client._pods["ac1"]["on"] is True, "power was flipped inside the off-time window"
    suppressed = [o for o in outcomes if o.fired and o.suppressed_reason]
    assert any("off-time" in o.suppressed_reason for o in suppressed)


# --- clock-jump robustness of the hysteresis gate (Qodo 3581287821) ---------


def test_forward_wall_clock_jump_within_one_process_cannot_bypass_gate(tmp_path: Path) -> None:
    """A forward wall-clock jump (NTP correction / clock skew) inside ONE process
    must not weaken short-cycling protection.

    The wall clock alone would report the off-time elapsed and permit an early
    power flip; the per-process monotonic clock has not advanced that far, so it
    must keep the gate closed.
    """
    db = tmp_path / "sensibo.db"
    rules_path = tmp_path / "rules.json"
    _seed_pod(db)

    rules = RulesStore(rules_path)
    rules.add(_threshold_rule("cool-on", "ac1", on=True, op=">", value=24))
    rules.add(_threshold_rule("cool-off", "ac1", on=False, op="<", value=22))
    _arm(rules, "cool-on")
    _arm(rules, "cool-off")

    client = _FakeClient({"ac1": {"on": False, "mode": "cool", "targetTemperature": 20}})
    min_off = 600.0

    # Pass 1: hot -> turn on. Wall clock 1000, monotonic 5000.
    with Store(db_path=db) as store:
        store.record_reading("ac1", "temperature", 26.0, timestamp=1000.0)
        run_once(store, rules, client, now_ts=1000.0, mono_ts=5000.0, min_off_time=min_off)
    assert client._pods["ac1"]["on"] is True

    # Pass 2: cold -> cool-off wants power off. The WALL clock leaps ~100000s
    # forward (well past the off-time), but only 60s of monotonic time actually
    # passed in this process. The gate must still block the power flip.
    with Store(db_path=db) as store:
        store.record_reading("ac1", "temperature", 20.0, timestamp=101000.0)
        outcomes = run_once(
            store, rules, client, now_ts=101000.0, mono_ts=5060.0, min_off_time=min_off
        )
    assert client._pods["ac1"]["on"] is True, "forward wall-clock jump bypassed the gate"
    suppressed = [o for o in outcomes if o.fired and o.suppressed_reason]
    assert any("off-time" in (o.suppressed_reason or "") for o in suppressed)


def test_future_persisted_stamp_suppresses_and_clamps_remaining(tmp_path: Path) -> None:
    """A persisted last-power-change that lies in the FUTURE (the wall clock was
    set backwards after the write) must SUPPRESS a power flip, never permit one
    on a negative elapsed, and report a remaining time clamped to the off-time
    window rather than ballooning by the size of the backwards jump.
    """
    db = tmp_path / "sensibo.db"
    rules_path = tmp_path / "rules.json"
    _seed_pod(db)
    with Store(db_path=db) as store:
        store.record_reading("ac1", "temperature", 20.0, timestamp=1000.0)

    rules = RulesStore(rules_path)
    rules.add(_threshold_rule("cool-off", "ac1", on=False, op="<", value=22))
    stored = rules.get("cool-off")

    # Future timestamp: the last power change is recorded at 2000, but "now" is 1000.
    rules.record_power_change("ac1", 2000.0)

    with Store(db_path=db) as store:
        report = dry_run(store, rules, stored, now_ts=1000.0, min_off_time=600.0)
    gate = report["power_gate"]
    assert gate["would_suppress"] is True
    # Clamped: without clamping, remaining would be 600 - (1000 - 2000) = 1600.
    assert 0.0 <= gate["remaining_seconds"] <= 600.0

    # The write path refuses too: it must not flip on a (negative) elapsed.
    rules.record_dry_run("cool-off")
    rules.arm("cool-off")
    client = _FakeClient({"ac1": {"on": True, "mode": "cool", "targetTemperature": 20}})
    with Store(db_path=db) as store:
        run_once(store, rules, client, now_ts=1000.0, min_off_time=600.0)
    assert client._pods["ac1"]["on"] is True, "future stamp permitted an early flip"


def test_restart_without_monotonic_stamp_enforces_wall_clock_minimum(tmp_path: Path) -> None:
    """After a restart there is NO in-process monotonic stamp, so the persisted
    wall-clock timestamp is the sole guard. It must both BLOCK a too-early flip
    and ALLOW one once the wall-clock off-time has genuinely elapsed.
    """
    db = tmp_path / "sensibo.db"
    rules_path = tmp_path / "rules.json"
    _seed_pod(db)

    rules = RulesStore(rules_path)
    rules.add(_threshold_rule("cool-on", "ac1", on=True, op=">", value=24))
    rules.add(_threshold_rule("cool-off", "ac1", on=False, op="<", value=22))
    _arm(rules, "cool-on")
    _arm(rules, "cool-off")

    client = _FakeClient({"ac1": {"on": False, "mode": "cool", "targetTemperature": 20}})
    with Store(db_path=db) as store:
        store.record_reading("ac1", "temperature", 26.0, timestamp=1000.0)
        run_once(store, rules, client, now_ts=1000.0, mono_ts=5000.0, min_off_time=600.0)
    assert client._pods["ac1"]["on"] is True

    # Restart: a brand-new store from the same file has no in-process monotonic
    # stamp for ac1 (it is deliberately not persisted).
    rebooted = RulesStore(rules_path)
    assert rebooted.monotonic_power_change("ac1") is None

    # 300s later (wall) the flip is refused. The wildly-large mono_ts would, if
    # consulted, say plenty of time passed — but monotonic is NOT consulted
    # across a restart, so the wall-clock minimum governs and blocks.
    with Store(db_path=db) as store:
        store.record_reading("ac1", "temperature", 20.0, timestamp=1300.0)
        run_once(store, rebooted, client, now_ts=1300.0, mono_ts=999999.0, min_off_time=600.0)
    assert client._pods["ac1"]["on"] is True, "flip allowed inside the wall-clock off-time"

    # 700s later (> 600) the wall-clock off-time has genuinely elapsed: allow it.
    # (mono_ts=1.0 is ignored because there is still no stamp for this pod.)
    with Store(db_path=db) as store:
        store.record_reading("ac1", "temperature", 20.0, timestamp=1700.0)
        run_once(store, rebooted, client, now_ts=1700.0, mono_ts=1.0, min_off_time=600.0)
    assert client._pods["ac1"]["on"] is False, "flip refused after the off-time elapsed"


def test_monotonic_power_change_is_recorded_in_process_but_not_persisted(tmp_path: Path) -> None:
    """The monotonic stamp guards clock jumps WITHIN one process; it is not
    comparable across processes, so it is held in memory only and a restart
    starts with none. The persisted wall-clock timestamp is what survives.
    """
    rules_path = tmp_path / "rules.json"
    rules = RulesStore(rules_path)
    rules.record_power_change("ac1", 1000.0, monotonic_ts=5000.0)

    assert rules.monotonic_power_change("ac1") == 5000.0
    assert rules.pod_state("ac1").last_power_change == 1000.0

    rebooted = RulesStore(rules_path)
    assert rebooted.monotonic_power_change("ac1") is None
    assert rebooted.pod_state("ac1").last_power_change == 1000.0


def test_one_pass_writes_each_pod_at_most_once(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    rules_path = tmp_path / "rules.json"
    _seed_pod(db)

    rules = RulesStore(rules_path)
    # Two rules that both fire on the same pod, requesting different changes.
    rules.add(
        Rule.from_dict(
            {
                "name": "first",
                "pod": "ac1",
                "action": {"on": True, "mode": "cool"},
                "conditions": {
                    "all": [
                        {
                            "type": "threshold",
                            "location": "ac1",
                            "field": "temperature",
                            "op": ">",
                            "value": 20,
                        }
                    ]
                },
            }
        )
    )
    rules.add(
        Rule.from_dict(
            {
                "name": "second",
                "pod": "ac1",
                "action": {"mode": "heat", "targetTemperature": 30},
                "conditions": {
                    "all": [
                        {
                            "type": "threshold",
                            "location": "ac1",
                            "field": "temperature",
                            "op": ">",
                            "value": 20,
                        }
                    ]
                },
            }
        )
    )
    _arm(rules, "first")
    _arm(rules, "second")

    client = _FakeClient({"ac1": {"on": False, "mode": "cool", "targetTemperature": 20}})
    with Store(db_path=db) as store:
        store.record_reading("ac1", "temperature", 26.0, timestamp=1000.0)
        outcomes = run_once(store, rules, client, now_ts=1000.0)

    writes = [c for c in client.calls if c[0] in ("patch_ac_state", "post_ac_states")]
    assert len(writes) == 1, "a single pass wrote the same pod more than once"
    suppressed = [o for o in outcomes if o.fired and not o.wrote]
    assert any("already wrote this pod" in (o.suppressed_reason or "") for o in suppressed)


# --- arm requires a fresh dry-run (and edits invalidate it) ------------------


def test_cannot_arm_without_a_dry_run(tmp_path: Path) -> None:
    rules = RulesStore(tmp_path / "rules.json")
    rules.add(_threshold_rule("r", "ac1", on=True, op=">", value=24))

    assert rules.can_arm("r") is False
    rules.record_dry_run("r")
    assert rules.can_arm("r") is True


def test_editing_a_rule_invalidates_its_dry_run(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.json"
    rules = RulesStore(rules_path)
    rules.add(_threshold_rule("r", "ac1", on=True, op=">", value=24))
    rules.record_dry_run("r")
    assert rules.can_arm("r") is True

    # Re-adding under the same name is an edit: it lands disarmed with no
    # fingerprint, so the stale dry-run cannot arm the changed rule.
    rules.add(_threshold_rule("r", "ac1", on=True, op=">", value=30))
    assert rules.can_arm("r") is False

    # And the persisted state agrees after a restart.
    assert RulesStore(rules_path).can_arm("r") is False


def test_fingerprint_gate_catches_a_definition_change_directly() -> None:
    rule_a = _threshold_rule("r", "ac1", on=True, op=">", value=24)
    rule_b = _threshold_rule("r", "ac1", on=True, op=">", value=30)
    assert rule_a.fingerprint() != rule_b.fingerprint()

    # A StoredRule whose recorded fingerprint no longer matches its rule is not
    # armable, even if the fingerprint field were carried over from before.
    stale = StoredRule(rule=rule_b, dry_run_fingerprint=rule_a.fingerprint())
    assert stale.is_dry_run_current() is False
    fresh = StoredRule(rule=rule_b, dry_run_fingerprint=rule_b.fingerprint())
    assert fresh.is_dry_run_current() is True


# --- cross-room condition resolves locations by alias ------------------------


def _seed_cross_room(db: Path, *, motion: bool, temp: float) -> None:
    with Store(db_path=db) as store:
        store.upsert_location("pod-bedroom", kind="pod", product_model="airq")
        store.set_alias("pod-bedroom", "Bedroom")
        store.record_reading("pod-bedroom", "temperature", temp, timestamp=500.0)
        store.upsert_location(
            "ms-hall", kind="room_sensor", parent_pod_id="pod-bedroom", seen_at=500.0
        )
        store.set_alias("ms-hall", "Hallway")
        store.record_reading("ms-hall", "motion", motion, timestamp=500.0)


def test_cross_room_condition_resolves_locations_by_alias(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    rule = Rule.from_dict(
        {
            "name": "cool-bedroom-when-hallway-busy",
            "pod": "pod-bedroom",
            "action": {"on": True, "mode": "cool", "targetTemperature": 22},
            "conditions": {
                "all": [
                    {"type": "occupancy", "location": "Hallway", "occupied": True},
                    {
                        "type": "threshold",
                        "location": "Bedroom",
                        "field": "temperature",
                        "op": ">",
                        "value": 26,
                    },
                ]
            },
        }
    )

    # motion in Hallway AND Bedroom hot -> fires (addressing both rooms by alias)
    _seed_cross_room(db, motion=True, temp=27.0)
    with Store(db_path=db) as store:
        assert evaluate(store, rule.conditions, now_ts=500.0).met is True

    # Hallway empty -> does not fire, even though the Bedroom is still hot.
    db2 = tmp_path / "sensibo2.db"
    _seed_cross_room(db2, motion=False, temp=27.0)
    with Store(db_path=db2) as store:
        assert evaluate(store, rule.conditions, now_ts=500.0).met is False


# --- each leaf condition type ------------------------------------------------


def test_threshold_condition(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    with Store(db_path=db) as store:
        store.upsert_location("ac1", kind="pod")
        store.record_reading("ac1", "temperature", 27.0, timestamp=10.0)
        cond_hot = {
            "type": "threshold",
            "location": "ac1",
            "field": "temperature",
            "op": ">",
            "value": 26,
        }
        cond_cold = {
            "type": "threshold",
            "location": "ac1",
            "field": "temperature",
            "op": "<",
            "value": 26,
        }
        assert evaluate(store, cond_hot, now_ts=10.0).met is True
        assert evaluate(store, cond_cold, now_ts=10.0).met is False


def test_threshold_with_no_reading_is_unmet_not_an_error(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    with Store(db_path=db) as store:
        store.upsert_location("ac1", kind="pod")
        cond = {
            "type": "threshold",
            "location": "ac1",
            "field": "temperature",
            "op": ">",
            "value": 10,
        }
        result = evaluate(store, cond, now_ts=10.0)
        assert result.met is False
        assert "no reading" in result.detail


def test_occupancy_condition(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    with Store(db_path=db) as store:
        store.upsert_location("ms1", kind="room_sensor", parent_pod_id="ac1")
        store.record_reading("ms1", "motion", True, timestamp=10.0)
        occupied = {"type": "occupancy", "location": "ms1", "occupied": True}
        vacant = {"type": "occupancy", "location": "ms1", "occupied": False}
        assert evaluate(store, occupied, now_ts=10.0).met is True
        assert evaluate(store, vacant, now_ts=10.0).met is False


def test_time_window_condition(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    with Store(db_path=db) as store:
        store.upsert_location("ac1", kind="pod")
        daytime = datetime.datetime(2026, 1, 1, 14, 30).timestamp()
        night = datetime.datetime(2026, 1, 1, 23, 0).timestamp()
        window = {"type": "time_window", "start": "08:00", "end": "18:00"}
        assert evaluate(store, window, now_ts=daytime).met is True
        assert evaluate(store, window, now_ts=night).met is False


def test_time_window_wraps_past_midnight(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    with Store(db_path=db) as store:
        store.upsert_location("ac1", kind="pod")
        window = {"type": "time_window", "start": "22:00", "end": "06:00"}
        late = datetime.datetime(2026, 1, 1, 23, 30).timestamp()
        early = datetime.datetime(2026, 1, 1, 3, 0).timestamp()
        noon = datetime.datetime(2026, 1, 1, 12, 0).timestamp()
        assert evaluate(store, window, now_ts=late).met is True
        assert evaluate(store, window, now_ts=early).met is True
        assert evaluate(store, window, now_ts=noon).met is False


# --- dry-run is read-only ----------------------------------------------------


def test_dry_run_touches_no_client_and_reports_would_fire(tmp_path: Path) -> None:
    db = tmp_path / "sensibo.db"
    rules_path = tmp_path / "rules.json"
    with Store(db_path=db) as store:
        store.upsert_location("ac1", kind="pod")
        store.record_reading("ac1", "temperature", 27.0, timestamp=10.0)

    rules = RulesStore(rules_path)
    rule = _threshold_rule("hot", "ac1", on=True, op=">", value=26)
    rules.add(rule)
    stored = rules.get("hot")

    # dry_run has no client parameter at all — it cannot write by construction.
    with Store(db_path=db) as store:
        report = dry_run(store, rules, stored, now_ts=10.0)
    assert report["would_fire"] is True
    assert report["execution"] == "local (stops when this daemon stops)"
    assert report["action_changes_power"] is True
    # dry_run's signature takes no client — writing is impossible by construction.
    assert "client" not in inspect.signature(dry_run).parameters


# --- validation --------------------------------------------------------------


def test_invalid_rule_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuleValidationError):
        Rule.from_dict({"name": "x", "pod": "ac1", "action": {}, "conditions": {}})
    with pytest.raises(RuleValidationError):
        Rule.from_dict(
            {
                "name": "x",
                "pod": "ac1",
                "action": {"on": True},
                "conditions": {
                    "all": [
                        {
                            "type": "threshold",
                            "location": "ac1",
                            "field": "t",
                            "op": "??",
                            "value": 1,
                        }
                    ]
                },
            }
        )


def test_min_off_time_is_floored_at_ten_minutes() -> None:
    assert effective_min_off_time(None) == float(DEFAULT_MIN_OFF_TIME_SECONDS)
    assert effective_min_off_time(60) == float(MIN_OFF_TIME_FLOOR_SECONDS)
    assert effective_min_off_time(1200) == 1200.0
