"""The local rules engine: evaluate conditions and (only when armed) drive the AC.

**This is the code that can start a compressor in someone's home.** Two safety
properties are built in and are the reason this module exists:

1. **Minimum off-time / hysteresis (per pod).** A rule may only flip a pod's
   power state if at least ``min_off_time`` seconds have passed since that pod's
   power last changed. The last-power-change timestamp is read from — and
   written back to — :class:`sensibo.rules.persistence.RulesStore`, so the gate
   holds across restarts. A condition that flaps (oscillating on/off every
   evaluation) therefore cannot cycle the compressor faster than
   ``min_off_time``; ``tests/test_rules_engine.py`` proves it.

   **The gate is robust to wall-clock jumps** (NTP steps, manual clock changes;
   Qodo review 3581287821). A naive gate that trusts only wall time can be
   fooled: a *forward* jump makes the elapsed time look larger and would permit
   an early power flip. So the gate consults TWO clocks and opens only when
   BOTH agree the off-time has elapsed:

   * the **persisted wall-clock** ``last_power_change`` — the only timestamp
     that can survive a restart, but not monotonic (an NTP step moves it);
   * an in-process **monotonic** stamp per pod
     (:meth:`~sensibo.rules.persistence.RulesStore.monotonic_power_change`),
     recorded alongside the wall-clock stamp on every power change. It cannot
     run backwards or leap, so within one process it blocks a flip that a
     forward wall-clock jump would otherwise wave through. It is *not* persisted
     (a monotonic zero is not comparable across processes), so after a restart —
     when no monotonic stamp exists for a pod — the wall-clock minimum is the
     sole guard, exactly as before.

   A persisted wall-clock stamp that lies in the FUTURE (the clock was set
   *backwards* after the write) is clamped to "now" so the elapsed time is 0
   rather than negative: the flip is suppressed, and the reported remaining time
   stays within the off-time window instead of ballooning by the jump size.
2. **At most one write per pod per pass.** :func:`run_once` writes to any given
   pod at most once, no matter how many armed rules target it, and every write
   goes through :class:`sensibo.api.SensiboClient` — inheriting its client-side
   pacing and 429 backoff.

``dry_run`` is strictly read-only: it evaluates a rule against the store and
reports what it WOULD do (and whether the hysteresis gate would currently block
a power change) without constructing or touching a client at all. ``run_once``
is the only function here that writes, and only for rules the operator has
explicitly armed (arming itself requires a fresh dry-run — see
:mod:`sensibo.rules.persistence`).

The engine never writes to a stream: it returns structured outcomes and lets
the CLI layer log them to stderr (the stream contract lives in
:mod:`sensibo.cli._output`, which this package must not import).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from sensibo.store import Store

from .evaluate import ConditionResult, evaluate
from .model import EXECUTION_FIELD, EXECUTION_LOCAL
from .persistence import RulesStore, StoredRule

#: Default minimum off-time between power-state changes on one pod: 10 minutes.
#: A caller may raise this but the engine floors any lower request at this value
#: so a misconfiguration can never make short-cycling protection weaker than the
#: documented minimum.
DEFAULT_MIN_OFF_TIME_SECONDS = 600
MIN_OFF_TIME_FLOOR_SECONDS = 600

_POWER_FIELD = "on"


class _AcClient(Protocol):
    """The slice of :class:`sensibo.api.SensiboClient` the engine needs."""

    def get_pod(self, pod_id: str, fields: str | None = ...) -> object: ...

    def patch_ac_state(
        self, pod_id: str, prop: str, current_ac_state: dict[str, Any], new_value: object
    ) -> object: ...

    def post_ac_states(self, pod_id: str, ac_state: dict[str, Any]) -> object: ...


@dataclass
class Outcome:
    """What one armed rule did (or didn't do) in one evaluation pass."""

    rule_name: str
    pod: str
    fired: bool
    wrote: bool = False
    method: str | None = None
    changes: dict[str, Any] = field(default_factory=dict)
    suppressed_reason: str | None = None
    condition: ConditionResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_name,
            "pod": self.pod,
            "fired": self.fired,
            "wrote": self.wrote,
            "method": self.method,
            "changes": self.changes,
            "suppressed_reason": self.suppressed_reason,
            "condition": self.condition.to_dict() if self.condition else None,
            EXECUTION_FIELD: EXECUTION_LOCAL,
        }


def effective_min_off_time(requested: float | None) -> float:
    """Clamp a requested minimum off-time up to the documented floor."""
    if requested is None:
        return float(DEFAULT_MIN_OFF_TIME_SECONDS)
    return float(max(requested, MIN_OFF_TIME_FLOOR_SECONDS))


# -- dry run (read-only) ------------------------------------------------------


def dry_run(
    data_store: Store,
    rules_store: RulesStore,
    stored: StoredRule,
    *,
    now_ts: float | None = None,
    mono_ts: float | None = None,
    min_off_time: float | None = None,
) -> dict[str, Any]:
    """Evaluate ``stored`` against the store NOW and report what it would do.

    Never constructs a client and never writes anything — the inspection mode a
    rule must pass before it can be armed. ``now_ts`` injects the wall clock and
    ``mono_ts`` the monotonic clock (both default to the real clocks; tests
    pin them).
    """
    now = time.time() if now_ts is None else now_ts
    mono_now = time.monotonic() if mono_ts is None else mono_ts
    floor = effective_min_off_time(min_off_time)
    rule = stored.rule
    condition = evaluate(data_store, rule.conditions, now_ts=now)

    changes_power = _POWER_FIELD in rule.action
    gate = _power_gate_status(rules_store, rule.pod, now=now, mono_now=mono_now, min_off_time=floor)

    report: dict[str, Any] = {
        "rule": rule.name,
        "pod": rule.pod,
        "armed": stored.armed,
        "would_fire": condition.met,
        "action": dict(rule.action),
        "action_changes_power": changes_power,
        "condition": condition.to_dict(),
        "condition_trace": condition.render_lines(),
        "power_gate": gate,
        "min_off_time_seconds": floor,
        EXECUTION_FIELD: EXECUTION_LOCAL,
    }
    return report


def _gate_remaining(
    rules_store: RulesStore, pod: str, *, now: float, mono_now: float, min_off_time: float
) -> tuple[float | None, float]:
    """Seconds still to wait before a power flip is allowed on ``pod``.

    Returns ``(last_power_change, remaining)``. ``remaining <= 0`` means the gate
    is open; ``last`` is ``None`` when this pod's power has never changed (no
    prior write to gate against). The result is the MAX of the wall-clock and —
    when a monotonic stamp exists this process lifetime — the monotonic
    outstanding waits, so a forward wall-clock jump cannot open the gate while
    the monotonic clock still says the window is open. A persisted stamp that
    lies in the future is clamped to "now" (elapsed 0), so it suppresses rather
    than permitting a flip on a negative elapsed. See this module's docstring
    (Qodo 3581287821) for the full reasoning.
    """
    last = rules_store.pod_state(pod).last_power_change
    if last is None:
        return None, 0.0
    wall_elapsed = max(0.0, now - last)
    remaining = min_off_time - wall_elapsed
    mono_last = rules_store.monotonic_power_change(pod)
    if mono_last is not None:
        mono_elapsed = max(0.0, mono_now - mono_last)
        remaining = max(remaining, min_off_time - mono_elapsed)
    return last, remaining


def _power_gate_status(
    rules_store: RulesStore, pod: str, *, now: float, mono_now: float, min_off_time: float
) -> dict[str, Any]:
    last, remaining = _gate_remaining(
        rules_store, pod, now=now, mono_now=mono_now, min_off_time=min_off_time
    )
    if last is None:
        return {"last_power_change": None, "would_suppress": False, "remaining_seconds": 0.0}
    if remaining > 0:
        return {
            "last_power_change": last,
            "would_suppress": True,
            "remaining_seconds": remaining,
        }
    return {"last_power_change": last, "would_suppress": False, "remaining_seconds": 0.0}


# -- run (the only writer) ----------------------------------------------------


def run_once(
    data_store: Store,
    rules_store: RulesStore,
    client: _AcClient,
    *,
    now_ts: float | None = None,
    mono_ts: float | None = None,
    min_off_time: float | None = None,
) -> list[Outcome]:
    """Evaluate every ARMED rule and apply at most one write per pod.

    Returns one :class:`Outcome` per armed rule (in stored order). The caller
    (``sensibo rule run``) logs the ones that wrote or were suppressed. ``now_ts``
    injects the wall clock and ``mono_ts`` the monotonic clock (both default to
    the real clocks; tests pin them). In a daemon loop the SAME ``rules_store``
    is reused across passes, which is what lets the per-process monotonic stamp
    guard the gate against a wall-clock jump between passes.
    """
    now = time.time() if now_ts is None else now_ts
    mono_now = time.monotonic() if mono_ts is None else mono_ts
    floor = effective_min_off_time(min_off_time)
    written_pods: set[str] = set()
    outcomes: list[Outcome] = []

    for stored in rules_store.armed_rules():
        outcomes.append(
            _evaluate_and_maybe_write(
                data_store,
                rules_store,
                client,
                stored,
                now=now,
                mono_now=mono_now,
                min_off_time=floor,
                written_pods=written_pods,
            )
        )
    return outcomes


def _evaluate_and_maybe_write(
    data_store: Store,
    rules_store: RulesStore,
    client: _AcClient,
    stored: StoredRule,
    *,
    now: float,
    mono_now: float,
    min_off_time: float,
    written_pods: set[str],
) -> Outcome:
    rule = stored.rule
    condition = evaluate(data_store, rule.conditions, now_ts=now)
    outcome = Outcome(rule_name=rule.name, pod=rule.pod, fired=condition.met, condition=condition)
    if not condition.met:
        return outcome

    if rule.pod in written_pods:
        outcome.suppressed_reason = "another rule already wrote this pod this pass"
        return outcome

    current = _current_ac_state(client, rule.pod)
    diff = _diff(current, rule.action)
    if not diff:
        outcome.suppressed_reason = "pod already in the desired state"
        return outcome

    changes_power = _POWER_FIELD in diff
    if changes_power:
        blocked = _hysteresis_block(
            rules_store, rule.pod, now=now, mono_now=mono_now, min_off_time=min_off_time
        )
        if blocked is not None:
            outcome.suppressed_reason = blocked
            outcome.changes = diff
            return outcome

    outcome.method = _apply(client, rule.pod, current, diff)
    outcome.wrote = True
    outcome.changes = diff
    written_pods.add(rule.pod)
    if changes_power:
        # Stamp BOTH clocks: the persisted wall clock (survives a restart) and
        # the in-process monotonic clock (immune to a wall-clock jump).
        rules_store.record_power_change(rule.pod, now, monotonic_ts=mono_now)
    else:
        rules_store.record_action(rule.pod, now)
    return outcome


def _hysteresis_block(
    rules_store: RulesStore, pod: str, *, now: float, mono_now: float, min_off_time: float
) -> str | None:
    """Return a suppression reason if a power change is inside the off-time window.

    Uses the two-clock gate (:func:`_gate_remaining`) so a forward wall-clock
    jump cannot open the window while the monotonic clock still says it is
    closed.
    """
    last, remaining = _gate_remaining(
        rules_store, pod, now=now, mono_now=mono_now, min_off_time=min_off_time
    )
    if last is None or remaining <= 0:
        return None
    return (
        f"minimum off-time not elapsed: {remaining:.0f}s remaining of "
        f"{min_off_time:.0f}s (prevents compressor short-cycling)"
    )


def _current_ac_state(client: _AcClient, pod_id: str) -> dict[str, Any]:
    result = _unwrap(client.get_pod(pod_id, fields="acState"))
    ac_state = result.get("acState") if isinstance(result, dict) else None
    return ac_state if isinstance(ac_state, dict) else {}


def _diff(current: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in action.items() if current.get(k) != v}


def _apply(client: _AcClient, pod_id: str, current: dict[str, Any], diff: dict[str, Any]) -> str:
    """Write ``diff`` to the pod; single field -> PATCH, multiple -> POST (like ``set``)."""
    if len(diff) == 1:
        ((prop, value),) = diff.items()
        client.patch_ac_state(pod_id, prop, current, value)
        return "patch"
    merged = dict(current)
    merged.update(diff)
    client.post_ac_states(pod_id, merged)
    return "post"


def _unwrap(response: object) -> object:
    if isinstance(response, dict) and "result" in response:
        return response["result"]
    return response
