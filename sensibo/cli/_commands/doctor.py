"""``sensibo doctor`` — check the agent-identity invariants.

Mirrors the two invariants ``steward doctor`` verifies for a mesh agent:

* **prompt-file-present** — the repo declares an agent in ``culture.yaml`` and
  has the matching prompt file on disk;
* **backend-consistency** — the declared ``backend`` matches the prompt file
  (``claude`` → ``CLAUDE.md``, ``colleague`` → ``AGENTS.colleague.md``,
  ``acp`` → ``AGENTS.md``, ``gemini`` → ``GEMINI.md``).

Plus a **skills-present** check (the vendored ``.claude/skills/`` kit) and a
**collector_heartbeat** check (task t6): reads the ``last_cycle_at`` /
``last_cycle_outcome`` meta keys ``sensibo collect`` writes each cycle and
reports unhealthy once the heartbeat is older than 3x the poll interval, or
absent. Read-only.

Reports the rubric-shaped contract
``{healthy, checks: [{id, passed, severity, message, remediation}]}`` so the
agent-first rubric's bundle 7 passes. Only ``error``-severity checks
(``prompt_file_present``, ``backend_consistency``) gate the top-level
``healthy``/exit code — ``skills_present`` and ``collector_heartbeat`` are
``warning`` severity and report themselves honestly without flipping a fresh
checkout's exit code. When run from a wheel install (no ``culture.yaml``
alongside the package), it skips the identity checks but still runs
``collector_heartbeat``.
"""

from __future__ import annotations

import argparse
import datetime
import time

from sensibo.cli._commands.whoami import find_culture_yaml, read_agent_fields
from sensibo.cli._output import emit_result
from sensibo.collect import DEFAULT_INTERVAL, META_COLLECT_INTERVAL
from sensibo.store import Store, StoreVersionError

# backend → required prompt file (the backend-consistency mapping).
_PROMPT_FILE = {
    "claude": "CLAUDE.md",
    "colleague": "AGENTS.colleague.md",
    "acp": "AGENTS.md",
    "gemini": "GEMINI.md",
}

_HEARTBEAT_REMEDIATION = "start sensibo collect --daemon"


def _parse_meta_timestamp(raw: str | None) -> float | None:
    """Parse a stored ``last_cycle_at`` meta value: epoch seconds or ISO 8601.

    The collector (a sibling task) owns the write side; this side stays
    liberal about the exact string shape rather than coupling to it.
    """
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _parse_interval(raw: str | None) -> float:
    """The daemon's own poll interval, or :data:`DEFAULT_INTERVAL` if unknown.

    A non-positive or unparseable value falls back too: this check must never
    divide an operator's freshness bound down to nothing on a corrupt meta row.
    """
    if raw is None:
        return float(DEFAULT_INTERVAL)
    try:
        interval = float(raw)
    except ValueError:
        return float(DEFAULT_INTERVAL)
    return interval if interval > 0 else float(DEFAULT_INTERVAL)


def _collector_heartbeat_check(db_path: str | None) -> dict[str, object]:
    """Is the collector still cycling? Reads the ``last_cycle_at`` / ``last_cycle_outcome``
    meta keys a sibling task's ``sensibo collect`` writes each cycle.

    Healthy iff a heartbeat is present and no older than 3x the poll interval
    — three missed cycles, not one, so a single slow poll never flaps this
    check. The interval is the daemon's own, read from the ``collect_interval``
    meta key it publishes each cycle, falling back to
    :data:`sensibo.collect.DEFAULT_INTERVAL` when no daemon has ever run:
    an operator who deliberately polls every 10 minutes must not be told their
    collector is dead four minutes after each cycle. Severity is ``warning``, not
    ``error``: a fresh checkout or a machine that has simply never run
    ``collect`` is not an identity-invariant failure, so this check reports
    itself unhealthy without pulling overall ``doctor`` healthy down with it
    (see ``_diagnose``'s severity-gated healthy computation).
    """
    try:
        with Store(db_path=db_path) as store:
            last_cycle_at = store.get_meta("last_cycle_at")
            last_cycle_outcome = store.get_meta("last_cycle_outcome")
            configured_interval = store.get_meta(META_COLLECT_INTERVAL)
    except StoreVersionError as err:
        return {
            "id": "collector_heartbeat",
            "passed": False,
            "severity": "warning",
            "message": f"could not read the collector heartbeat: {err}",
            "remediation": err.remediation,
        }

    interval = _parse_interval(configured_interval)
    ts = _parse_meta_timestamp(last_cycle_at)
    healthy = ts is not None and (time.time() - ts) <= 3 * interval
    if last_cycle_at is None:
        message = (
            f"no collector heartbeat recorded yet (last_cycle_at unset); {interval:g}s cadence"
        )
    else:
        message = (
            f"last_cycle_at={last_cycle_at} last_cycle_outcome={last_cycle_outcome} "
            f"interval={interval:g}s (stale after {3 * interval:g}s)"
        )
    return {
        "id": "collector_heartbeat",
        "passed": healthy,
        "severity": "warning",
        "message": message,
        "remediation": "" if healthy else _HEARTBEAT_REMEDIATION,
    }


def _diagnose(db_path: str | None = None) -> dict[str, object]:
    cfg = find_culture_yaml()
    if cfg is None:
        checks: list[dict[str, object]] = [
            {
                "id": "source_checkout",
                "passed": True,
                "severity": "info",
                "message": "no culture.yaml found alongside the package; identity checks skipped",
                "remediation": "",
            }
        ]
        checks.append(_collector_heartbeat_check(db_path))
        healthy = all(c["passed"] for c in checks if c["severity"] == "error")
        return {"healthy": healthy, "checks": checks}

    root = cfg.parent
    fields = read_agent_fields()
    backend = fields["backend"]
    checks = []

    # 1. backend-consistency: the prompt file for the declared backend exists.
    expected = _PROMPT_FILE.get(backend)
    if expected is None:
        checks.append(
            {
                "id": "backend_consistency",
                "passed": False,
                "severity": "error",
                "message": f"unknown backend '{backend}' in culture.yaml",
                "remediation": f"set backend to one of: {', '.join(sorted(_PROMPT_FILE))}",
            }
        )
    else:
        present = (root / expected).is_file()
        checks.append(
            {
                "id": "prompt_file_present",
                "passed": present,
                "severity": "error",
                "message": (
                    f"backend '{backend}' requires {expected} — "
                    + ("present" if present else "missing")
                ),
                "remediation": "" if present else f"create {expected} at the repo root",
            }
        )

    # 2. skills-present: the vendored skill kit is on disk.
    skills_dir = root / ".claude" / "skills"
    has_skills = skills_dir.is_dir() and any(skills_dir.iterdir())
    checks.append(
        {
            "id": "skills_present",
            "passed": has_skills,
            "severity": "warning",
            "message": (
                ".claude/skills/ vendored" if has_skills else ".claude/skills/ missing or empty"
            ),
            "remediation": (
                "" if has_skills else "vendor the skill kit (see docs/skill-sources.md)"
            ),
        }
    )

    checks.append(_collector_heartbeat_check(db_path))

    # Only `error`-severity checks gate overall `healthy` — `warning` (and
    # `info`) checks report themselves honestly (e.g. collector_heartbeat can
    # be `passed: False` on a fresh checkout with no collector ever run) but
    # never flip the exit code that the identity-invariant checks own.
    healthy = all(c["passed"] for c in checks if c["severity"] == "error")
    return {"healthy": healthy, "checks": checks}


def cmd_doctor(args: argparse.Namespace) -> int:
    report = _diagnose(db_path=getattr(args, "db", None))
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(report, json_mode=True)
    else:
        status = "healthy" if report["healthy"] else "unhealthy"
        lines = [f"sensibo doctor: {status}", ""]
        for check in report["checks"]:
            mark = "ok" if check["passed"] else "FAIL"
            lines.append(f"[{mark}] {check['id']}: {check['message']}")
            if not check["passed"] and check["remediation"]:
                lines.append(f"  hint: {check['remediation']}")
        emit_result("\n".join(lines), json_mode=False)
    return 0 if report["healthy"] else 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "doctor",
        help="Check the agent-identity invariants (prompt-file-present, backend-consistency).",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help=(
            "Store path for the collector_heartbeat check "
            "(else SENSIBO_DB, else ~/.sensibo/sensibo.db)."
        ),
    )
    p.set_defaults(func=cmd_doctor)
