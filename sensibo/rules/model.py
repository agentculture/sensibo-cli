"""The declarative rule data model (task t9).

A rule is **data, not code**: a name, a target pod, a desired ``action``
(``acState`` changes), and a tree of ``conditions`` evaluated against the
CURRENT readings in the local store. This module owns the shape of that data,
its validation, and its stable fingerprint — nothing here talks to the network,
the store, or the CLI (``sensibo.rules`` imports :mod:`sensibo.store` and
:mod:`sensibo.api`, never :mod:`sensibo.cli`; see ``sensibo/rules/__init__.py``).

**Local execution is part of the definition.** Every rule carries an
``execution`` marker — :data:`EXECUTION_LOCAL` — so a rule (and every verb that
renders one) is unambiguous that it runs on THIS operator's machine and stops
when the local daemon stops. That is the deliberate contrast with Sensibo's
cloud-executed automation (``smartmode``/``schedule``/``timer`` — see
:mod:`sensibo.cli._cloud`), which keeps enforcing itself while this machine
sleeps.

Condition grammar (all leaves address a location BY NAME — id, operator alias,
or Sensibo room name — resolved through :func:`sensibo.store.resolve_location`,
which is what makes cross-room rules possible):

* combinators — ``{"all": [<cond>, ...]}``, ``{"any": [<cond>, ...]}``,
  ``{"not": <cond>}``;
* ``{"type": "threshold", "location": <name>, "field": <name>,
  "op": <one of > >= < <= == !=>, "value": <number>}``;
* ``{"type": "occupancy", "location": <name>, "occupied": <bool>,
  "field": <optional; defaults to motion/roomIsOccupied>}``;
* ``{"type": "time_window", "start": "HH:MM", "end": "HH:MM"}`` (wraps midnight
  when ``start > end``);
* ``{"type": "stale", "location": <name>, "after_seconds": <optional positive
  int>}`` — true when the location's persisted health (written by the collector,
  read back through :meth:`sensibo.store.Store.get_health`) is anything but
  ``ok``, or, when ``after_seconds`` is given, when its last good reading is
  older than that. A location with no health row at all counts as stale:
  silence we cannot explain is not evidence of health. This is the leaf that
  lets a rule refuse to trust a dead room's motion or temperature.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

#: The ``execution`` field every rule definition and every rule-verb output
#: carries. Kept here (in the engine package, not the CLI) so the definition
#: itself is self-describing to any consumer that reads a raw rule file.
EXECUTION_FIELD = "execution"
EXECUTION_LOCAL = "local (stops when this daemon stops)"

#: Comparison operators a ``threshold`` condition may use.
THRESHOLD_OPS = (">", ">=", "<", "<=", "==", "!=")

#: Condition ``type`` values a leaf may declare.
LEAF_TYPES = ("threshold", "occupancy", "time_window", "stale")

#: The exact key set a ``stale`` leaf may carry. Unlike the older leaves this
#: one is closed: a typo'd key here would silently widen or narrow the set of
#: conditions under which a rule drives a compressor, so it is rejected at
#: validation rather than ignored at evaluation.
STALE_KEYS = frozenset({"type", "location", "after_seconds"})

#: Occupancy fields tried, in order, when a rule's occupancy condition does not
#: name a specific field. A Room Sensor reports ``motion``; a pod may surface
#: ``roomIsOccupied``.
OCCUPANCY_FIELDS = ("motion", "roomIsOccupied")

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class RuleError(Exception):
    """Base class for rule-model failures.

    Mirrors the layering choice in :mod:`sensibo.store.rooms`: the engine
    raises its own exception types rather than :class:`sensibo.cli._errors.CliError`
    so the CLI layer decides the exit code and remediation text.
    """


class RuleValidationError(RuleError):
    """A rule (or one of its conditions) is structurally invalid."""


@dataclass(frozen=True)
class Rule:
    """A single declarative rule: what to do, to which pod, under what conditions."""

    name: str
    pod: str
    action: dict[str, Any]
    conditions: dict[str, Any]

    def to_definition(self) -> dict[str, Any]:
        """The canonical, serialisable definition — always carrying the local marker."""
        return {
            "name": self.name,
            "pod": self.pod,
            "action": _deep_copy(self.action),
            "conditions": _deep_copy(self.conditions),
            EXECUTION_FIELD: EXECUTION_LOCAL,
        }

    def fingerprint(self) -> str:
        """A stable content hash of this rule's definition.

        Two rules with identical name/pod/action/conditions share a
        fingerprint; changing ANY of those changes it. This is the hook the
        arm-requires-a-fresh-dry-run gate hangs on (see
        :mod:`sensibo.rules.persistence`): a rule armed against one fingerprint
        is disarmed the moment its definition — and therefore its fingerprint —
        changes.
        """
        canonical = json.dumps(self.to_definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Any) -> "Rule":
        """Validate ``data`` and build a :class:`Rule`, or raise :class:`RuleValidationError`."""
        if not isinstance(data, dict):
            raise RuleValidationError("a rule must be a JSON object")
        name = _require_nonempty_str(data.get("name"), "name")
        pod = _require_nonempty_str(data.get("pod"), "pod")

        action = data.get("action")
        if not isinstance(action, dict) or not action:
            raise RuleValidationError(f"rule {name!r}: 'action' must be a non-empty object")
        _validate_action(action, name)

        conditions = data.get("conditions")
        if not isinstance(conditions, dict):
            raise RuleValidationError(f"rule {name!r}: 'conditions' must be an object")
        _validate_condition(conditions, name)

        return cls(name=name, pod=pod, action=dict(action), conditions=_deep_copy(conditions))


# -- validation helpers -------------------------------------------------------


def _require_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuleValidationError(f"'{field}' must be a non-empty string")
    return value


def _validate_action(action: dict[str, Any], rule_name: str) -> None:
    # An action is a set of acState fields. The only value shape we constrain is
    # the safety-relevant 'on' field, which must be a real bool (power state).
    if "on" in action and not isinstance(action["on"], bool):
        raise RuleValidationError(
            f"rule {rule_name!r}: action 'on' must be a boolean (the power state)"
        )


def _validate_condition(cond: Any, rule_name: str) -> None:
    if not isinstance(cond, dict):
        raise RuleValidationError(f"rule {rule_name!r}: each condition must be an object")

    combinators = [key for key in ("all", "any", "not") if key in cond]
    has_type = "type" in cond
    if combinators and has_type:
        raise RuleValidationError(
            f"rule {rule_name!r}: a condition is either a combinator or a leaf, not both"
        )
    if len(combinators) > 1:
        raise RuleValidationError(
            f"rule {rule_name!r}: a condition may use only one of all/any/not"
        )

    if combinators:
        _validate_combinator(cond, combinators[0], rule_name)
        return
    if has_type:
        _validate_leaf(cond, rule_name)
        return
    raise RuleValidationError(
        f"rule {rule_name!r}: a condition needs 'all'/'any'/'not' or a 'type'"
    )


def _validate_combinator(cond: dict[str, Any], key: str, rule_name: str) -> None:
    value = cond[key]
    if key == "not":
        _validate_condition(value, rule_name)
        return
    if not isinstance(value, list) or not value:
        raise RuleValidationError(f"rule {rule_name!r}: '{key}' must be a non-empty list")
    for child in value:
        _validate_condition(child, rule_name)


def _validate_leaf(cond: dict[str, Any], rule_name: str) -> None:
    kind = cond.get("type")
    if kind not in LEAF_TYPES:
        raise RuleValidationError(
            f"rule {rule_name!r}: unknown condition type {kind!r} "
            f"(expected one of {', '.join(LEAF_TYPES)})"
        )
    if kind == "threshold":
        _require_nonempty_str(cond.get("location"), "location")
        _require_nonempty_str(cond.get("field"), "field")
        if cond.get("op") not in THRESHOLD_OPS:
            raise RuleValidationError(
                f"rule {rule_name!r}: threshold 'op' must be one of {', '.join(THRESHOLD_OPS)}"
            )
        if isinstance(cond.get("value"), bool) or not isinstance(cond.get("value"), (int, float)):
            raise RuleValidationError(f"rule {rule_name!r}: threshold 'value' must be a number")
    elif kind == "occupancy":
        _require_nonempty_str(cond.get("location"), "location")
        if "occupied" in cond and not isinstance(cond["occupied"], bool):
            raise RuleValidationError(f"rule {rule_name!r}: occupancy 'occupied' must be a boolean")
        if "field" in cond:
            _require_nonempty_str(cond.get("field"), "field")
    elif kind == "time_window":
        _validate_hhmm(cond.get("start"), "start", rule_name)
        _validate_hhmm(cond.get("end"), "end", rule_name)
    elif kind == "stale":
        _validate_stale(cond, rule_name)


def _validate_stale(cond: dict[str, Any], rule_name: str) -> None:
    unknown = sorted(set(cond) - STALE_KEYS)
    if unknown:
        raise RuleValidationError(
            f"rule {rule_name!r}: stale condition has unknown key(s): {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(STALE_KEYS))})"
        )
    _require_nonempty_str(cond.get("location"), "location")
    if "after_seconds" in cond:
        value = cond["after_seconds"]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuleValidationError(
                f"rule {rule_name!r}: stale 'after_seconds' must be a positive whole "
                "number of seconds"
            )


def _validate_hhmm(value: Any, field: str, rule_name: str) -> None:
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise RuleValidationError(
            f"rule {rule_name!r}: time_window '{field}' must be 24-hour HH:MM"
        )


def _deep_copy(value: Any) -> Any:
    """A JSON-shaped deep copy (rules are always plain JSON data)."""
    if isinstance(value, dict):
        return {key: _deep_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy(item) for item in value]
    return value
