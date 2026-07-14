"""``sensibo rule`` — the local, declarative rules engine (task t9).

**This noun is the only thing in the CLI that can start a compressor from a
condition.** Its safety model is deliberately different from Sensibo's
cloud-executed automation (``smartmode``/``schedule``/``timer``) and from the
one-shot ``set`` verb:

* a rule is inert until **armed**, and a rule cannot be armed until a
  ``rule dry-run`` has evaluated its CURRENT definition (editing the rule
  invalidates that — the fingerprint changes);
* ``rule dry-run`` is strictly read-only — it evaluates against the local store
  and prints exactly what the rule would do, touching no client;
* ``rule run`` is the ONLY verb that drives an AC, and only for armed rules. It
  applies at most one write per pod per pass, and a per-pod minimum off-time
  (>= 10 minutes, persisted across restarts) stops a flapping condition
  short-cycling the compressor.

Every rule, and every line this noun prints, declares
``execution: local (stops when this daemon stops)`` — this engine runs on THIS
machine and stops when the daemon does, unlike the cloud verbs.

``rule add``/``remove``/``arm``/``disarm`` edit the local rules file only (never
the AC), so they act immediately; the AC-driving gate is arming + ``run``.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from sensibo.api import ApiError
from sensibo.cli._apierrors import from_api_error
from sensibo.cli._commands._client import build_client
from sensibo.cli._commands.overview import emit_overview
from sensibo.cli._errors import EXIT_USER_ERROR, CliError
from sensibo.cli._output import emit_diagnostic, emit_result
from sensibo.rules import (
    EXECUTION_FIELD,
    EXECUTION_LOCAL,
    THRESHOLD_OPS,
    Outcome,
    Rule,
    RulesStore,
    RuleValidationError,
    StoredRule,
    dry_run,
    run_once,
)
from sensibo.store import Store

_EXECUTION_LINE = f"{EXECUTION_FIELD}: {EXECUTION_LOCAL}"
_MIN_RUN_INTERVAL_SECONDS = 5.0


def _execution_marker() -> dict[str, str]:
    return {EXECUTION_FIELD: EXECUTION_LOCAL}


# -- rules-file / store plumbing ---------------------------------------------


def _rules_store(args: argparse.Namespace) -> RulesStore:
    return RulesStore(getattr(args, "rules", None))


def _require_rule(store: RulesStore, name: str) -> StoredRule:
    stored = store.get(name)
    if stored is None:
        known = ", ".join(sr.rule.name for sr in store.list_rules()) or "(none)"
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"no rule named {name!r}",
            remediation=f"list rules with 'sensibo rule list'; known: {known}",
        )
    return stored


# -- rule add: build a Rule from --file or inline flags ----------------------


def _rule_from_file(path: str) -> Rule:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as err:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"cannot read rule file {path!r}: {err.strerror or err}",
            remediation="pass a readable JSON file with --file",
        ) from err
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"rule file {path!r} is not valid JSON: {err}",
            remediation="fix the JSON syntax; see examples/cross-room-motion-temp.rule.json",
        ) from err
    return _validate_rule(data)


def _rule_from_flags(args: argparse.Namespace) -> Rule:
    action = _action_from_flags(args)
    if not action:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="no action given for the rule",
            remediation="pass at least one of --power/--mode/--target/--fan/--swing (or --file)",
        )
    conditions = _conditions_from_flags(args)
    data = {
        "name": args.name,
        "pod": args.pod,
        "action": action,
        "conditions": conditions,
    }
    return _validate_rule(data)


def _action_from_flags(args: argparse.Namespace) -> dict[str, Any]:
    action: dict[str, Any] = {}
    if args.power is not None:
        action["on"] = args.power == "on"
    if args.mode is not None:
        action["mode"] = args.mode
    if args.target is not None:
        action["targetTemperature"] = args.target
    if args.fan is not None:
        action["fanLevel"] = args.fan
    if args.swing is not None:
        action["swing"] = args.swing
    return action


def _conditions_from_flags(args: argparse.Namespace) -> dict[str, Any]:
    parts = [args.when_location, args.when_field, args.when_op, args.when_value]
    given = [p for p in parts if p is not None]
    if not given:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="no condition given for the rule",
            remediation=(
                "pass a threshold with --when-location/--when-field/--when-op/--when-value, "
                "or build a richer (cross-room) rule with --file"
            ),
        )
    if len(given) != 4:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="an inline threshold needs all of --when-location/--when-field/"
            "--when-op/--when-value",
            remediation="supply all four flags, or use --file for a richer condition tree",
        )
    return {
        "all": [
            {
                "type": "threshold",
                "location": args.when_location,
                "field": args.when_field,
                "op": args.when_op,
                "value": args.when_value,
            }
        ]
    }


def _validate_rule(data: Any) -> Rule:
    try:
        return Rule.from_dict(data)
    except RuleValidationError as err:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid rule: {err}",
            remediation="fix the rule definition; see 'sensibo explain rule add'",
        ) from err


def cmd_add(args: argparse.Namespace) -> int:
    rule = _rule_from_file(args.file) if args.file else _rule_from_flags(args)
    store = _rules_store(args)
    replaced = store.get(rule.name) is not None
    store.add(rule)

    payload = {
        "action": "add",
        "rule": rule.to_definition(),
        "replaced": replaced,
        "armed": False,
        "next": f"run 'sensibo rule dry-run {rule.name}' before arming",
        **_execution_marker(),
    }
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        verb = "replaced" if replaced else "added"
        emit_result(
            f"{verb} rule '{rule.name}' -> pod {rule.pod}\n"
            f"{_EXECUTION_LINE}\n"
            f"disarmed: run 'sensibo rule dry-run {rule.name}', then 'sensibo rule arm "
            f"{rule.name}' to activate",
            json_mode=False,
        )
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    store = _rules_store(args)
    _require_rule(store, args.name)
    store.remove(args.name)
    json_mode = bool(getattr(args, "json", False))
    payload = {"action": "remove", "rule": args.name, "removed": True, **_execution_marker()}
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(f"removed rule '{args.name}'\n{_EXECUTION_LINE}", json_mode=False)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = _rules_store(args)
    rules = store.list_rules()
    json_mode = bool(getattr(args, "json", False))

    rows = []
    for sr in rules:
        rows.append(
            {
                "name": sr.rule.name,
                "pod": sr.rule.pod,
                "armed": sr.armed,
                "dry_run_current": sr.is_dry_run_current(),
                "action": dict(sr.rule.action),
            }
        )
    if json_mode:
        emit_result({"rules": rows, **_execution_marker()}, json_mode=True)
    else:
        emit_result(_render_list(rows), json_mode=False)
    return 0


def _render_list(rows: list[dict[str, Any]]) -> str:
    lines = ["sensibo rule list", _EXECUTION_LINE, ""]
    if not rows:
        lines.append("(no rules defined — add one with 'sensibo rule add')")
        return "\n".join(lines)
    for row in rows:
        armed = "ARMED" if row["armed"] else "disarmed"
        dry = "dry-run:current" if row["dry_run_current"] else "dry-run:stale/none"
        lines.append(f"{row['name']}  pod={row['pod']}  {armed}  {dry}  action={row['action']}")
    return "\n".join(lines)


def cmd_dry_run(args: argparse.Namespace) -> int:
    rules = _rules_store(args)
    stored = _require_rule(rules, args.name)
    with Store(db_path=args.db) as data_store:
        report = dry_run(data_store, rules, stored, min_off_time=args.min_off_time)
    # Persisting the fingerprint is what unlocks arming: a rule can only be
    # armed against a dry-run of its CURRENT definition.
    rules.record_dry_run(args.name)

    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(report, json_mode=True)
    else:
        emit_result(_render_dry_run(report), json_mode=False)
    return 0


def _render_dry_run(report: dict[str, Any]) -> str:
    lines = [
        f"sensibo rule dry-run: {report['rule']} -> pod {report['pod']}",
        _EXECUTION_LINE,
        "",
        f"would fire now: {'YES' if report['would_fire'] else 'no'}",
        f"action if fired: {report['action']}",
    ]
    gate = report["power_gate"]
    if report["action_changes_power"]:
        if gate["would_suppress"]:
            lines.append(
                f"power gate: a power change now WOULD be suppressed "
                f"({gate['remaining_seconds']:.0f}s of the "
                f"{report['min_off_time_seconds']:.0f}s minimum off-time remain)"
            )
        else:
            lines.append("power gate: a power change now would be allowed")
    lines.append("")
    lines.append("conditions:")
    lines.extend(f"  {line}" for line in report["condition_trace"])
    lines.append("")
    lines.append("dry-run: read-only, nothing was written. Arm with 'sensibo rule arm <name>'.")
    return "\n".join(lines)


def cmd_arm(args: argparse.Namespace) -> int:
    store = _rules_store(args)
    stored = _require_rule(store, args.name)
    if not store.can_arm(args.name):
        never = stored.dry_run_fingerprint is None
        reason = (
            "it has never been dry-run"
            if never
            else "its definition changed since the last dry-run"
        )
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"cannot arm rule {args.name!r}: {reason}",
            remediation=f"run 'sensibo rule dry-run {args.name}' first, then arm it",
        )
    store.arm(args.name)
    json_mode = bool(getattr(args, "json", False))
    payload = {"action": "arm", "rule": args.name, "armed": True, **_execution_marker()}
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(
            f"armed rule '{args.name}' — 'sensibo rule run' will now drive its pod\n"
            f"{_EXECUTION_LINE}",
            json_mode=False,
        )
    return 0


def cmd_disarm(args: argparse.Namespace) -> int:
    store = _rules_store(args)
    _require_rule(store, args.name)
    store.disarm(args.name)
    json_mode = bool(getattr(args, "json", False))
    payload = {"action": "disarm", "rule": args.name, "armed": False, **_execution_marker()}
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(f"disarmed rule '{args.name}'\n{_EXECUTION_LINE}", json_mode=False)
    return 0


# -- rule run: the only AC-driving verb --------------------------------------


def _log_outcomes(outcomes: list[Outcome]) -> None:
    """Log each write / suppression to stderr, tagged with the rule name."""
    for outcome in outcomes:
        if outcome.wrote:
            emit_diagnostic(
                f"rule '{outcome.rule_name}': applied {outcome.changes} to pod "
                f"{outcome.pod} via {outcome.method}"
            )
        elif outcome.fired and outcome.suppressed_reason:
            emit_diagnostic(
                f"rule '{outcome.rule_name}': fired but suppressed "
                f"({outcome.suppressed_reason})"
            )


def _run_pass(data_store: Store, rules: RulesStore, args: argparse.Namespace) -> list[Outcome]:
    client = build_client()
    try:
        return run_once(data_store, rules, client, min_off_time=args.min_off_time)
    except ApiError as err:
        raise from_api_error(err) from err


def _emit_run_summary(outcomes: list[Outcome], *, json_mode: bool) -> None:
    if json_mode:
        emit_result(
            {"outcomes": [o.to_dict() for o in outcomes], **_execution_marker()},
            json_mode=True,
        )
        return
    wrote = sum(1 for o in outcomes if o.wrote)
    fired = sum(1 for o in outcomes if o.fired)
    emit_result(
        f"sensibo rule run: evaluated {len(outcomes)} armed rule(s), "
        f"{fired} fired, {wrote} write(s)\n{_EXECUTION_LINE}",
        json_mode=False,
    )


def cmd_run(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    rules = _rules_store(args)

    if args.daemon:
        return _run_daemon(rules, args, json_mode=json_mode)

    with Store(db_path=args.db) as data_store:
        outcomes = _run_pass(data_store, rules, args)
    _log_outcomes(outcomes)
    _emit_run_summary(outcomes, json_mode=json_mode)
    return 0


def _run_daemon(rules: RulesStore, args: argparse.Namespace, *, json_mode: bool) -> int:
    interval = max(float(args.interval), _MIN_RUN_INTERVAL_SECONDS)
    emit_diagnostic(
        f"sensibo rule run --daemon: evaluating every {interval:.0f}s "
        f"({EXECUTION_LOCAL}); Ctrl-C to stop"
    )
    try:
        while True:
            with Store(db_path=args.db) as data_store:
                outcomes = _run_pass(data_store, rules, args)
            _log_outcomes(outcomes)
            time.sleep(interval)
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        emit_diagnostic("sensibo rule run --daemon: stopped")
        return 0


# -- overview / registration -------------------------------------------------


def _rule_sections() -> list[dict[str, object]]:
    return [
        {
            "title": "Verbs",
            "items": [
                "rule list — every rule with its armed / dry-run state",
                "rule add --file F | --name ... --pod ... --power ... --when-... — define a rule",
                "rule remove <name> — delete a rule",
                "rule dry-run <name> — evaluate NOW against the store; read-only",
                "rule arm <name> — activate a rule (requires a fresh dry-run)",
                "rule disarm <name> — deactivate a rule",
                "rule run [--once | --daemon --interval S] — drive armed rules' pods",
                "rule overview — describe this noun (this command)",
            ],
        },
        {
            "title": "Safety",
            "items": [
                "a rule is inert until armed; arming requires a fresh dry-run of its "
                "current definition (editing it invalidates that)",
                "per-pod minimum off-time (>= 10 min, persisted across restarts) prevents "
                "compressor short-cycling",
                "one evaluation pass writes each pod at most once; writes use the API "
                "client's own rate limiting",
            ],
        },
        {"title": "Execution", "items": [_EXECUTION_LINE]},
    ]


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "sensibo rule",
        _rule_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_overview(args)


def _add_rules_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--rules",
        default=None,
        metavar="PATH",
        help="Override the rules file (else SENSIBO_RULES, else ~/.sensibo/rules.json).",
    )


def _add_db_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Override the store path (else SENSIBO_DB, else ~/.sensibo/sensibo.db).",
    )


def _add_min_off_time(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--min-off-time",
        type=float,
        default=None,
        dest="min_off_time",
        metavar="SECONDS",
        help="Minimum seconds between power changes on one pod (floored at 600 = 10 min).",
    )


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "rule",
        help="Local declarative automation that drives the AC (see 'sensibo rule overview').",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="rule_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the rule noun.")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_overview)

    lst = noun_sub.add_parser("list", help="List every rule with its armed / dry-run state.")
    _add_rules_flag(lst)
    lst.add_argument("--json", action="store_true", help="Emit structured JSON.")
    lst.set_defaults(func=cmd_list)

    add = noun_sub.add_parser("add", help="Define a rule from a JSON file or inline flags.")
    add.add_argument("--file", default=None, help="JSON file describing the rule.")
    add.add_argument("--name", default=None, help="Rule name (inline).")
    add.add_argument("--pod", default=None, help="Target pod id (inline).")
    add.add_argument("--power", choices=("on", "off"), default=None, help="Action: power state.")
    add.add_argument("--mode", default=None, help="Action: AC mode (cool/heat/...).")
    add.add_argument("--target", type=int, default=None, help="Action: target temperature.")
    add.add_argument("--fan", default=None, help="Action: fan level.")
    add.add_argument("--swing", default=None, help="Action: swing mode.")
    add.add_argument(
        "--when-location",
        dest="when_location",
        default=None,
        help="Inline threshold: location (id, alias, or Sensibo room name).",
    )
    add.add_argument(
        "--when-field",
        dest="when_field",
        default=None,
        help="Inline threshold: reading field, e.g. temperature.",
    )
    add.add_argument(
        "--when-op",
        dest="when_op",
        choices=THRESHOLD_OPS,
        default=None,
        help="Inline threshold: comparison operator.",
    )
    add.add_argument(
        "--when-value",
        dest="when_value",
        type=float,
        default=None,
        help="Inline threshold: comparison value.",
    )
    _add_rules_flag(add)
    add.add_argument("--json", action="store_true", help="Emit structured JSON.")
    add.set_defaults(func=cmd_add)

    rm = noun_sub.add_parser("remove", help="Delete a rule.")
    rm.add_argument("name", help="The rule name to remove.")
    _add_rules_flag(rm)
    rm.add_argument("--json", action="store_true", help="Emit structured JSON.")
    rm.set_defaults(func=cmd_remove)

    dry = noun_sub.add_parser("dry-run", help="Evaluate a rule NOW against the store (read-only).")
    dry.add_argument("name", help="The rule name to evaluate.")
    _add_rules_flag(dry)
    _add_db_flag(dry)
    _add_min_off_time(dry)
    dry.add_argument("--json", action="store_true", help="Emit structured JSON.")
    dry.set_defaults(func=cmd_dry_run)

    arm = noun_sub.add_parser("arm", help="Activate a rule (requires a fresh dry-run).")
    arm.add_argument("name", help="The rule name to arm.")
    _add_rules_flag(arm)
    arm.add_argument("--json", action="store_true", help="Emit structured JSON.")
    arm.set_defaults(func=cmd_arm)

    disarm = noun_sub.add_parser("disarm", help="Deactivate a rule.")
    disarm.add_argument("name", help="The rule name to disarm.")
    _add_rules_flag(disarm)
    disarm.add_argument("--json", action="store_true", help="Emit structured JSON.")
    disarm.set_defaults(func=cmd_disarm)

    run = noun_sub.add_parser(
        "run", help="Evaluate armed rules and drive their pods (--once or --daemon)."
    )
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Evaluate one pass, then exit (default).")
    mode.add_argument("--daemon", action="store_true", help="Loop, evaluating every --interval.")
    run.add_argument(
        "--interval",
        type=float,
        default=90.0,
        metavar="SECONDS",
        help="Daemon evaluation interval (floored at 5s; default 90).",
    )
    _add_rules_flag(run)
    _add_db_flag(run)
    _add_min_off_time(run)
    run.add_argument("--json", action="store_true", help="Emit structured JSON.")
    run.set_defaults(func=cmd_run)
