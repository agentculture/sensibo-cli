"""The health evaluator: one pure function, replayable and clock-free.

:func:`evaluate` takes the health map as it stood, what this poll cycle saw,
whether the cycle itself succeeded, and the instant it happened; it returns the
new map plus the transitions to log and the notifications to send. It reads no
clock, opens no file, and makes no call — the collector (task t5) supplies the
inputs and persists the outputs, so a test can replay a month of cycles in
milliseconds and a daemon restart is just "load the map back and carry on".

Three things it deliberately does NOT do, because they are not decidable here:
deduplicate against notifications already *delivered* (the state it is handed
carries that memory), decide retention, or format for a transport.

Order of judgement, per cycle:

1. **Collector first.** If the cycle failed, nothing can be said about any
   location: everything becomes ``unknown``, no sensor goes ``down``, and one
   ``collector_unhealthy`` notification fires on the ok -> failed edge only.
2. **Parents before children.** A Room Sensor is a BLE satellite; when its
   parent pod is down the child's silence is explained, so it becomes
   ``unknown_parent_down`` and never notifies. One pod outage is one alert, not
   N.
3. **Debounce.** A down fires the first cycle past the threshold (the threshold
   is already ~10 missed cycles); a recovery must hold for
   ``recovery_hold_cycles`` consecutive good evaluations. A per-location
   cooldown spaces repeat down alerts, and a daily cap is the hard ceiling.
   A recovery is announced only when the outage it closes was announced — that
   pairing is what keeps a flapping sensor to one down and one recovery per
   cooldown window, while a real battery pull always gets its closing message
   however quickly the battery goes back in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .model import (
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
    day_key,
    iso8601,
)


def evaluate(
    previous: Mapping[str, HealthState],
    observations: Sequence[Observation],
    collector: CollectorOutcome,
    now: float,
    config: HealthConfig | None = None,
    collector_previous_ok: bool | None = None,
) -> EvaluationResult:
    """Evaluate one cycle. Pure: same inputs, same outputs, always.

    ``previous`` is the persisted health map (empty on the very first run —
    every location is then seeded, and one down notification fires per location
    already past its threshold, so an outage that predates the upgrade is
    announced instead of being silently absorbed as the baseline).
    ``collector_previous_ok`` is the last cycle's collector verdict, ``None``
    when there is no history; it is what makes the collector notifications
    edge-triggered rather than per-cycle.

    Locations in ``previous`` but absent from ``observations`` are carried
    forward untouched — a fleet snapshot that omits a location says nothing
    about it, and deleting its history would lose an open outage.
    """
    settings = HealthConfig() if config is None else config
    states: dict[str, HealthState] = dict(previous)
    transitions: list[Transition] = []
    notifications: list[Notification] = []

    seen: dict[str, Observation] = {}
    for observation in observations:
        seen[observation.location_id] = observation

    if not collector.ok:
        _mark_all_unknown(states, seen, transitions, now)
        if collector_previous_ok is not False:
            notifications.append(_collector_note(collector, now, healthy=False))
        return EvaluationResult(
            states=states,
            transitions=tuple(transitions),
            notifications=tuple(notifications),
            suppressed_count=0,
            collector_ok=False,
        )

    if collector_previous_ok is False:
        notifications.append(_collector_note(collector, now, healthy=True))

    suppressed = 0
    today = day_key(now)
    raw: dict[str, str] = {
        location_id: _raw_status(observation, now, settings)
        for location_id, observation in seen.items()
    }
    for observation in _parents_first(seen):
        state, transition, candidate = _settle(
            previous.get(observation.location_id),
            observation,
            raw[observation.location_id],
            _parent_status(observation.parent_pod_id, states, raw),
            now,
            settings,
        )
        if transition is not None:
            transitions.append(transition)
        state, note, was_suppressed = _announce(state, candidate, observation, now, today, settings)
        if note is not None:
            notifications.append(note)
        suppressed += 1 if was_suppressed else 0
        states[observation.location_id] = state

    return EvaluationResult(
        states=states,
        transitions=tuple(transitions),
        notifications=tuple(notifications),
        suppressed_count=suppressed,
        collector_ok=True,
    )


# --- the collector-failure branch ------------------------------------------


def _carry_forward(prior: HealthState | None) -> dict[str, object]:
    """The cooldown/daily-cap/announcement memory, unchanged from ``prior``.

    Shared by both branches that build a :class:`HealthState` without
    themselves deciding anything about notification bookkeeping: the
    collector-failure path (:func:`_mark_all_unknown`) and the per-location
    settle path (:func:`_settle`).
    """
    return {
        "last_notified_at": prior.last_notified_at if prior is not None else None,
        "notifications_today": prior.notifications_today if prior is not None else 0,
        "day_key": prior.day_key if prior is not None else None,
        "announced_down_since": prior.announced_down_since if prior is not None else None,
    }


def _transition(
    location_id: str, prior: HealthState | None, to_status: str, now: float
) -> Transition:
    """Build the :class:`Transition` for a location settling on ``to_status``."""
    return Transition(
        location_id=location_id,
        from_status=prior.status if prior is not None else None,
        to_status=to_status,
        at=now,
    )


def _parent_for_unknown(observation: Observation | None, prior: HealthState | None) -> str | None:
    """The parent pod id to carry while a location is forced ``unknown``.

    Prefers this cycle's observation; falls back to what was already
    persisted so a pod that dropped out of the snapshot doesn't lose its
    children's lineage.
    """
    parent = observation.parent_pod_id if observation is not None else None
    if parent is None and prior is not None:
        parent = prior.parent_pod_id
    return parent


def _mark_all_unknown(
    states: dict[str, HealthState],
    seen: Mapping[str, Observation],
    transitions: list[Transition],
    now: float,
) -> None:
    """Every known location becomes ``unknown`` — never ``down``."""
    order = list(states) + [key for key in seen if key not in states]
    for location_id in order:
        prior = states.get(location_id)
        observation = seen.get(location_id)
        changed = prior is None or prior.status != STATUS_UNKNOWN
        if changed:
            transitions.append(_transition(location_id, prior, STATUS_UNKNOWN, now))
        states[location_id] = HealthState(
            location_id=location_id,
            status=STATUS_UNKNOWN,
            since=now if changed else prior.since,
            last_ok=prior.last_ok if prior is not None else None,
            parent_pod_id=_parent_for_unknown(observation, prior),
            ok_streak=0,
            **_carry_forward(prior),
        )


def _collector_note(collector: CollectorOutcome, now: float, *, healthy: bool) -> Notification:
    if healthy:
        return Notification(
            kind=NOTIFY_COLLECTOR_RECOVERED,
            location_id=None,
            message=(
                f"collector recovered at {iso8601(now)}: the fleet snapshot succeeded "
                "again and every location is being evaluated normally"
            ),
            at=now,
        )
    reason = collector.error or "the fleet snapshot failed"
    return Notification(
        kind=NOTIFY_COLLECTOR_UNHEALTHY,
        location_id=None,
        message=(
            f"collector unhealthy at {iso8601(now)}: {reason}. Every location is "
            "marked unknown; no sensor-down alert can be trusted until it recovers"
        ),
        at=now,
    )


# --- per-location judgement -------------------------------------------------


def _raw_status(observation: Observation, now: float, config: HealthConfig) -> str:
    """``down`` or ``ok``, from this location's own evidence alone.

    A location that has never reported at all is down: an id in the fleet with
    no reading behind it is exactly the battery-dead case.
    """
    if observation.is_alive is False:
        return STATUS_DOWN
    if observation.last_reading_at is None:
        return STATUS_DOWN
    if (now - observation.last_reading_at) > config.down_after_seconds:
        return STATUS_DOWN
    return STATUS_OK


def _parents_first(seen: Mapping[str, Observation]) -> list[Observation]:
    """Pods before their satellites, so a child sees its parent's settled status."""
    parents = [obs for obs in seen.values() if obs.parent_pod_id is None]
    children = [obs for obs in seen.values() if obs.parent_pod_id is not None]
    return parents + children


def _parent_status(
    parent_pod_id: str | None,
    states: Mapping[str, HealthState],
    raw: Mapping[str, str],
) -> str | None:
    """The parent pod's status this cycle, falling back to its persisted one.

    A pod absent from the snapshot but persisted as down still shelters its
    children: the outage did not end just because the pod stopped being listed.
    """
    if parent_pod_id is None:
        return None
    settled = states.get(parent_pod_id)
    if settled is not None:
        return settled.status
    return raw.get(parent_pod_id)


def _status_for_reporting(
    prior: HealthState | None, config: HealthConfig
) -> tuple[str, int, str | None]:
    """The status/streak/candidate when this cycle's own evidence is ``ok``.

    Split out of :func:`_status_for` purely to keep each function's branching
    small: this is the recovery-hold half of the judgement.
    """
    streak = (prior.ok_streak + 1) if prior is not None else 1
    if prior is None or prior.status == STATUS_OK:
        return STATUS_OK, streak, None
    if streak >= config.recovery_hold_cycles:
        # Closes an announced outage — even one that passed through
        # ``unknown`` while the collector itself was failing.
        candidate = NOTIFY_RECOVERED if prior.announced_down_since is not None else None
        return STATUS_OK, streak, candidate
    # Reporting again, but the hold is not satisfied: hold the line.
    return prior.status, streak, None


def _status_for(
    raw_status: str,
    parent_status: str | None,
    prior: HealthState | None,
    config: HealthConfig,
) -> tuple[str, int, str | None]:
    """The new status, ``ok_streak``, and a maybe-notify candidate.

    Pure judgement, no :class:`HealthState`/:class:`Transition` construction —
    that split is what keeps this and :func:`_settle` each under the
    complexity gate.
    """
    if parent_status == STATUS_DOWN:
        return STATUS_UNKNOWN_PARENT_DOWN, 0, None
    if raw_status == STATUS_DOWN:
        candidate = NOTIFY_DOWN if (prior is None or prior.status != STATUS_DOWN) else None
        return STATUS_DOWN, 0, candidate
    return _status_for_reporting(prior, config)


def _last_ok_for(prior: HealthState | None, raw_status: str, now: float) -> float | None:
    """The carried-forward ``last_ok`` instant, given this cycle's raw evidence."""
    if raw_status == STATUS_OK:
        return now
    if prior is not None:
        return prior.last_ok
    return None


def _settle(
    prior: HealthState | None,
    observation: Observation,
    raw_status: str,
    parent_status: str | None,
    now: float,
    config: HealthConfig,
) -> tuple[HealthState, Transition | None, str | None]:
    """Fold one observation into a state, a maybe-transition, a maybe-alert."""
    status, streak, candidate = _status_for(raw_status, parent_status, prior, config)

    changed = prior is None or prior.status != status
    transition = _transition(observation.location_id, prior, status, now) if changed else None
    state = HealthState(
        location_id=observation.location_id,
        status=status,
        since=now if changed else prior.since,
        last_ok=_last_ok_for(prior, raw_status, now),
        parent_pod_id=observation.parent_pod_id,
        ok_streak=streak,
        **_carry_forward(prior),
    )
    return state, transition, candidate


# --- debounce: cooldown and the daily cap ----------------------------------


def _announce(
    state: HealthState,
    candidate: str | None,
    observation: Observation,
    now: float,
    today: str,
    config: HealthConfig,
) -> tuple[HealthState, Notification | None, bool]:
    """Apply the cooldown and the daily cap to a candidate notification."""
    sent_today = 0 if state.day_key != today else state.notifications_today
    state = _replace(state, notifications_today=sent_today, day_key=today)
    if candidate is None:
        return state, None, False

    within_cooldown = (
        candidate == NOTIFY_DOWN
        and state.last_notified_at is not None
        and (now - state.last_notified_at) < config.cooldown_seconds
    )
    # The daily cap bounds *down* alerts; a recovery closes an outage that was
    # already announced, so it is bounded by those downs and never capped —
    # otherwise an announced outage could end with no closing message at all.
    capped = candidate == NOTIFY_DOWN and sent_today >= config.daily_cap
    if within_cooldown or capped:
        return state, None, True

    note = Notification(
        kind=candidate,
        location_id=observation.location_id,
        message=_message(candidate, observation, now, config),
        at=now,
    )
    announced = now if candidate == NOTIFY_DOWN else None
    state = _replace(
        state,
        last_notified_at=now,
        notifications_today=sent_today + 1,
        announced_down_since=announced,
    )
    return state, note, False


def _message(kind: str, observation: Observation, now: float, config: HealthConfig) -> str:
    heard = iso8601(observation.last_reading_at)
    label = observation.kind or "location"
    if kind == NOTIFY_DOWN:
        return (
            f"{label} {observation.location_id} is down as of {iso8601(now)}: "
            f"last heard {heard}, past the {int(config.down_after_seconds)}s threshold"
        )
    return (
        f"{label} {observation.location_id} recovered at {iso8601(now)}: "
        f"reporting again, latest reading {heard}"
    )


def _replace(state: HealthState, **changes: object) -> HealthState:
    values = state.to_dict()
    values.update(changes)
    return HealthState(**values)  # type: ignore[arg-type]
