"""Local persistence for rules, their armed state, and per-pod safety timestamps.

Everything a running rules engine must not forget lives in one JSON file the
operator owns — ``~/.sensibo/rules.json`` by default (override with the
``SENSIBO_RULES`` env var or an explicit path; tests always pass a ``tmp_path``,
never the real home file). Three things are persisted together:

1. **the rules themselves** — each rule's declarative definition plus its
   operational metadata (``armed``, and the ``dry_run_fingerprint`` recorded by
   the last ``rule dry-run``);
2. **the arm gate's evidence** — a rule may only be armed against a fingerprint
   that matches its CURRENT definition, so editing a rule silently invalidates a
   stale dry-run (see :meth:`RulesStore.can_arm`);
3. **per-pod safety timestamps** — the last time each pod's power state was
   changed and the last time any action was applied. These outlive a restart on
   purpose: the minimum-off-time hysteresis (:mod:`sensibo.rules.engine`) that
   stops a compressor short-cycling is only sound if a fresh process still
   remembers when the last power change happened.

Writes are atomic (temp file + :func:`os.replace`) and the parent directory is
created 0700, matching :class:`sensibo.store.Store`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import Rule

#: Env var that overrides the default rules-file path.
ENV_VAR = "SENSIBO_RULES"

_DEFAULT_RELATIVE = Path(".sensibo") / "rules.json"
_SCHEMA_VERSION = 1


def default_rules_path() -> Path:
    """The rules file implied by ``SENSIBO_RULES``, else ``~/.sensibo/rules.json``."""
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / _DEFAULT_RELATIVE


def resolve_rules_path(path: str | os.PathLike[str] | None) -> Path:
    """An explicit ``path`` wins; otherwise fall back to :func:`default_rules_path`."""
    if path is not None:
        return Path(path)
    return default_rules_path()


@dataclass
class StoredRule:
    """A rule plus its operational metadata as persisted on disk."""

    rule: Rule
    armed: bool = False
    dry_run_fingerprint: str | None = None

    def is_dry_run_current(self) -> bool:
        """True only when the recorded dry-run matches this rule's CURRENT definition."""
        return self.dry_run_fingerprint is not None and (
            self.dry_run_fingerprint == self.rule.fingerprint()
        )


@dataclass
class PodState:
    """Per-pod safety bookkeeping that must survive a restart."""

    last_power_change: float | None = None
    last_action: float | None = None


class RulesStore:
    """File-backed collection of rules, arm state, and per-pod safety timestamps.

    Every mutating method persists immediately, so a crash or restart never
    loses the arm state or — critically — the last-power-change timestamps the
    hysteresis gate depends on.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path: Path = resolve_rules_path(path)
        self._rules: dict[str, StoredRule] = {}
        self._pods: dict[str, PodState] = {}
        # Per-pod ``time.monotonic()`` stamp of the last power change, held in
        # memory only and deliberately NOT persisted. A monotonic clock is not
        # comparable across processes (its zero is arbitrary), so a fresh process
        # starts with none and the hysteresis gate falls back to the persisted
        # wall-clock timestamp. Within one process lifetime it is the guard that
        # a wall-clock (NTP) jump cannot fool — see :mod:`sensibo.rules.engine`.
        self._monotonic_power_change: dict[str, float] = {}
        self._load()

    # -- load / save ------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for entry in raw.get("rules", []):
            rule = Rule.from_dict(entry["definition"])
            self._rules[rule.name] = StoredRule(
                rule=rule,
                armed=bool(entry.get("armed", False)),
                dry_run_fingerprint=entry.get("dry_run_fingerprint"),
            )
        for pod_id, state in raw.get("pods", {}).items():
            self._pods[pod_id] = PodState(
                last_power_change=state.get("last_power_change"),
                last_action=state.get("last_action"),
            )

    def _save(self) -> None:
        doc: dict[str, Any] = {
            "version": _SCHEMA_VERSION,
            "rules": [
                {
                    "definition": sr.rule.to_definition(),
                    "armed": sr.armed,
                    "dry_run_fingerprint": sr.dry_run_fingerprint,
                }
                for sr in self._rules.values()
            ],
            "pods": {
                pod_id: {
                    "last_power_change": state.last_power_change,
                    "last_action": state.last_action,
                }
                for pod_id, state in self._pods.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- rules ------------------------------------------------------------

    def list_rules(self) -> list[StoredRule]:
        return list(self._rules.values())

    def get(self, name: str) -> StoredRule | None:
        return self._rules.get(name)

    def add(self, rule: Rule) -> None:
        """Insert (or replace by name) a rule.

        A replacement always lands DISARMED with no dry-run fingerprint: a new
        definition must earn a fresh dry-run before it can ever be armed, so an
        edit can never silently keep an old arm.
        """
        self._rules[rule.name] = StoredRule(rule=rule, armed=False, dry_run_fingerprint=None)
        self._save()

    def remove(self, name: str) -> bool:
        if name not in self._rules:
            return False
        del self._rules[name]
        self._save()
        return True

    def record_dry_run(self, name: str) -> str:
        """Stamp the rule's current fingerprint as its dry-run evidence."""
        sr = self._require(name)
        sr.dry_run_fingerprint = sr.rule.fingerprint()
        self._save()
        return sr.dry_run_fingerprint

    def can_arm(self, name: str) -> bool:
        """True only when the rule has a dry-run fingerprint matching its definition."""
        return self._require(name).is_dry_run_current()

    def arm(self, name: str) -> None:
        sr = self._require(name)
        sr.armed = True
        self._save()

    def disarm(self, name: str) -> None:
        sr = self._require(name)
        sr.armed = False
        self._save()

    def armed_rules(self) -> list[StoredRule]:
        return [sr for sr in self._rules.values() if sr.armed]

    # -- per-pod safety state --------------------------------------------

    def pod_state(self, pod_id: str) -> PodState:
        return self._pods.get(pod_id, PodState())

    def monotonic_power_change(self, pod_id: str) -> float | None:
        """The in-process ``time.monotonic()`` stamp of this pod's last power change.

        Returns ``None`` when this pod's power has not changed during the CURRENT
        process lifetime (e.g. right after a restart). Never persisted — see the
        note in :meth:`__init__`.
        """
        return self._monotonic_power_change.get(pod_id)

    def record_power_change(
        self, pod_id: str, ts: float, *, monotonic_ts: float | None = None
    ) -> None:
        state = self._pods.setdefault(pod_id, PodState())
        state.last_power_change = ts
        state.last_action = ts
        if monotonic_ts is not None:
            # In-memory only; intentionally not written by ``_save``.
            self._monotonic_power_change[pod_id] = monotonic_ts
        self._save()

    def record_action(self, pod_id: str, ts: float) -> None:
        state = self._pods.setdefault(pod_id, PodState())
        state.last_action = ts
        self._save()

    # -- internal ---------------------------------------------------------

    def _require(self, name: str) -> StoredRule:
        sr = self._rules.get(name)
        if sr is None:
            raise KeyError(name)
        return sr
