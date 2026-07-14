"""sensibo.rules — the local, declarative rules engine (task t9).

Conditions over the readings already collected in the local
:mod:`sensibo.store` drive an AC through :mod:`sensibo.api`. This is the
product's differentiator: a rule can combine conditions ACROSS rooms (``motion
in Hallway AND temperature in Bedroom > 26``) — addressing each location by name
through :func:`sensibo.store.resolve_location` — which Sensibo's per-device
Climate React cannot express.

**Layering rule (mirrors :mod:`sensibo.store`):** this package imports the store
and the API client, and is imported BY the CLI — it must never import
:mod:`sensibo.cli`. That keeps the engine usable as a library and keeps the
stream/error contracts owned by the CLI layer.

**Local execution, always.** Every rule and every rule-verb output carries
:data:`~sensibo.rules.model.EXECUTION_LOCAL` — ``"local (stops when this daemon
stops)"`` — the deliberate contrast with Sensibo's cloud-executed automation
(``smartmode``/``schedule``/``timer``), which keeps running while this machine
sleeps.

**Safety lives in :mod:`sensibo.rules.engine`.** A per-pod minimum off-time
(``>=`` 10 minutes, persisted across restarts) stops a flapping condition
short-cycling a compressor, one evaluation pass writes each pod at most once,
and every write goes through the API client's own rate limiting. Arming a rule
requires a fresh dry-run of its current definition; editing the rule
invalidates that.
"""

from __future__ import annotations

from .engine import (
    DEFAULT_MIN_OFF_TIME_SECONDS,
    MIN_OFF_TIME_FLOOR_SECONDS,
    Outcome,
    dry_run,
    effective_min_off_time,
    run_once,
)
from .evaluate import ConditionResult, evaluate
from .model import (
    EXECUTION_FIELD,
    EXECUTION_LOCAL,
    OCCUPANCY_FIELDS,
    THRESHOLD_OPS,
    Rule,
    RuleError,
    RuleValidationError,
)
from .persistence import (
    PodState,
    RulesStore,
    StoredRule,
    default_rules_path,
    resolve_rules_path,
)

__all__ = [
    "ConditionResult",
    "DEFAULT_MIN_OFF_TIME_SECONDS",
    "EXECUTION_FIELD",
    "EXECUTION_LOCAL",
    "MIN_OFF_TIME_FLOOR_SECONDS",
    "OCCUPANCY_FIELDS",
    "Outcome",
    "PodState",
    "Rule",
    "RuleError",
    "RuleValidationError",
    "RulesStore",
    "StoredRule",
    "THRESHOLD_OPS",
    "default_rules_path",
    "dry_run",
    "effective_min_off_time",
    "evaluate",
    "resolve_rules_path",
    "run_once",
]
