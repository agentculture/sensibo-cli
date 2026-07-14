"""Evaluate a rule's condition tree against the CURRENT store readings (task t9).

Pure over :mod:`sensibo.store`: every leaf reads the latest value the local
store holds for a field at a location, and every location is addressed BY NAME
and resolved through :func:`sensibo.store.resolve_location` — the same resolver
``room``/``query`` use, so a rule can say ``"temperature in Bedroom"`` and mean
the operator's alias, Sensibo's room name, or the stable id interchangeably.
That name resolution is exactly what lets one rule combine conditions across
rooms (``motion in Hallway AND temperature in Bedroom > 26``) — something
Sensibo's per-device Climate React cannot express.

Evaluation is **total and safe**: an unresolvable location or a missing reading
makes a leaf *unmet* (with an explanatory trace) rather than raising — a rule
must never drive a compressor on data it does not actually have. The nested
:class:`ConditionResult` doubles as the human-readable "why" that ``rule
dry-run`` prints.
"""

from __future__ import annotations

import datetime
import operator
from dataclasses import dataclass, field
from typing import Any, Callable

from sensibo.store import (
    AmbiguousLocationError,
    LocationNotFoundError,
    Store,
    resolve_location,
)

from .model import OCCUPANCY_FIELDS

_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}

_TRUTHY_STRINGS = {"true", "1", "yes", "on", "occupied", "motion", "detected"}


@dataclass
class ConditionResult:
    """Whether one (possibly nested) condition is met, plus a human "why"."""

    met: bool
    detail: str
    children: list["ConditionResult"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"met": self.met, "detail": self.detail}
        if self.children:
            out["children"] = [child.to_dict() for child in self.children]
        return out

    def render_lines(self, indent: int = 0) -> list[str]:
        mark = "MET " if self.met else "unmet"
        lines = [f"{'  ' * indent}[{mark}] {self.detail}"]
        for child in self.children:
            lines.extend(child.render_lines(indent + 1))
        return lines


def evaluate(store: Store, condition: dict[str, Any], *, now_ts: float) -> ConditionResult:
    """Evaluate a validated condition tree; never raises on missing data."""
    if "all" in condition:
        return _evaluate_all(store, condition["all"], now_ts=now_ts)
    if "any" in condition:
        return _evaluate_any(store, condition["any"], now_ts=now_ts)
    if "not" in condition:
        inner = evaluate(store, condition["not"], now_ts=now_ts)
        return ConditionResult(met=not inner.met, detail="NOT", children=[inner])
    return _evaluate_leaf(store, condition, now_ts=now_ts)


def _evaluate_all(store: Store, children: list[Any], *, now_ts: float) -> ConditionResult:
    results = [evaluate(store, child, now_ts=now_ts) for child in children]
    met = all(r.met for r in results)
    return ConditionResult(met=met, detail="ALL of", children=results)


def _evaluate_any(store: Store, children: list[Any], *, now_ts: float) -> ConditionResult:
    results = [evaluate(store, child, now_ts=now_ts) for child in children]
    met = any(r.met for r in results)
    return ConditionResult(met=met, detail="ANY of", children=results)


def _evaluate_leaf(store: Store, cond: dict[str, Any], *, now_ts: float) -> ConditionResult:
    kind = cond["type"]
    if kind == "threshold":
        return _evaluate_threshold(store, cond)
    if kind == "occupancy":
        return _evaluate_occupancy(store, cond)
    return _evaluate_time_window(cond, now_ts=now_ts)


def _resolve(store: Store, name: str) -> tuple[str | None, str]:
    """Resolve a location name to its id; return (id-or-None, note)."""
    try:
        loc = resolve_location(store, name)
        return loc.id, name
    except LocationNotFoundError:
        return None, f"no location matches {name!r}"
    except AmbiguousLocationError:
        return None, f"{name!r} matches more than one location"


def _evaluate_threshold(store: Store, cond: dict[str, Any]) -> ConditionResult:
    name = cond["location"]
    field_name = cond["field"]
    op_symbol = cond["op"]
    target = float(cond["value"])

    loc_id, note = _resolve(store, name)
    if loc_id is None:
        return ConditionResult(met=False, detail=f"threshold {field_name} in {name}: {note}")

    reading = store.latest_reading(loc_id, field_name)
    if reading is None:
        return ConditionResult(
            met=False,
            detail=f"threshold {field_name} in {name}: no reading stored yet",
        )
    numeric = _as_number(reading.value)
    if numeric is None:
        return ConditionResult(
            met=False,
            detail=f"threshold {field_name} in {name}: value {reading.value!r} is not numeric",
        )
    met = _OPS[op_symbol](numeric, target)
    return ConditionResult(
        met=met,
        detail=f"threshold {field_name} in {name}: {numeric:g} {op_symbol} {target:g}",
    )


def _evaluate_occupancy(store: Store, cond: dict[str, Any]) -> ConditionResult:
    name = cond["location"]
    want_occupied = bool(cond.get("occupied", True))
    fields = (cond["field"],) if cond.get("field") else OCCUPANCY_FIELDS

    loc_id, note = _resolve(store, name)
    if loc_id is None:
        return ConditionResult(met=False, detail=f"occupancy in {name}: {note}")

    for candidate in fields:
        reading = store.latest_reading(loc_id, candidate)
        if reading is None:
            continue
        occupied = _truthy(reading.value)
        met = occupied == want_occupied
        state = "occupied" if occupied else "vacant"
        return ConditionResult(
            met=met,
            detail=(
                f"occupancy in {name}: {candidate}={state}, "
                f"want {'occupied' if want_occupied else 'vacant'}"
            ),
        )
    tried = ", ".join(fields)
    return ConditionResult(
        met=False, detail=f"occupancy in {name}: no reading for any of [{tried}]"
    )


def _evaluate_time_window(cond: dict[str, Any], *, now_ts: float) -> ConditionResult:
    start = _hhmm_to_minutes(cond["start"])
    end = _hhmm_to_minutes(cond["end"])
    local = datetime.datetime.fromtimestamp(now_ts)
    now_minutes = local.hour * 60 + local.minute

    if start <= end:
        met = start <= now_minutes < end
    else:  # window wraps past midnight, e.g. 22:00 -> 06:00
        met = now_minutes >= start or now_minutes < end
    return ConditionResult(
        met=met,
        detail=(
            f"time_window {cond['start']}-{cond['end']}: " f"local time {local.strftime('%H:%M')}"
        ),
    )


def _hhmm_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _as_number(value: float | str) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: float | str) -> bool:
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in _TRUTHY_STRINGS
