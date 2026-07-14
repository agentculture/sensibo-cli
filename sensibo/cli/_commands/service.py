"""``sensibo service`` — install the always-on units: collection and the dashboard.

The three daemons this project ships are foreground processes. ``service``
puts the two read-shaped ones (``collect --daemon``, ``web``) under systemd as
**user** units with ``Restart=always``, and turns on ``loginctl`` lingering so
they start at **boot** without a login. That is the whole "so I can always come
and look" story, and the whole "a collection gap is lost data" defence — see
:mod:`sensibo.service`.

``rule run --daemon`` is **not** installed. It drives a compressor unattended;
arming that is an explicit operator decision, never a side effect of turning on
collection.

Write-verb contract, as everywhere else in this CLI: ``install`` and
``uninstall`` are **dry-run by default** and print exactly what they would do.
``--apply`` commits. The guarantee is structural — the plan builders in
:mod:`sensibo.service.manager` are pure, so the no-``--apply`` path never
reaches code that mutates the system.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from sensibo.cli._commands.overview import emit_overview
from sensibo.cli._errors import EXIT_ENV_ERROR, CliError
from sensibo.cli._output import emit_result
from sensibo.service import (
    ALL_UNITS,
    InstallPlan,
    ServiceError,
    apply_install,
    apply_uninstall,
    build_install_plan,
    build_uninstall_plan,
    default_runner,
    require_systemd,
    resolve_exec_path,
)
from sensibo.service import status as service_status
from sensibo.store import Store, default_db_path


def _runner():
    """Testing seam: the subprocess runner every systemd call routes through.

    Module-level (the same pattern as ``collect.build_client``) so a test can
    monkeypatch it — no test ever shells out to a real ``systemctl``.
    """
    return default_runner


EXECUTION_FIELD = "execution"
#: Deliberately distinct from `sensibo rule`'s "local (stops when this daemon
#: stops)" — installing these units is precisely what removes that caveat.
EXECUTION_SUPERVISED = "local (systemd-supervised; survives logout and reboot)"
_EXECUTION_LINE = f"{EXECUTION_FIELD}: {EXECUTION_SUPERVISED}"

_RULE_NOTE = (
    "'rule run --daemon' is NOT installed: it drives a compressor unattended, "
    "so arming it stays an explicit operator decision"
)

JSON_HELP = "Emit structured JSON."


def _as_cli_error(err: ServiceError) -> CliError:
    """Map the engine's error onto the CLI contract. Always environment-class."""
    return CliError(code=EXIT_ENV_ERROR, message=err.message, remediation=err.remediation)


def _unit_dir(args: argparse.Namespace) -> Path | None:
    raw = getattr(args, "unit_dir", None)
    return Path(raw).expanduser() if raw else None


# -- install ----------------------------------------------------------------


def _render_install_text(plan: InstallPlan, *, applied: bool, show_units: bool) -> str:
    lines = ["sensibo service install", _EXECUTION_LINE, ""]
    for warning in plan.warnings:
        lines.append(f"warning: {warning}")
    if plan.warnings:
        lines.append("")

    lines.append(f"exec:     {plan.exec_path}")
    lines.append(f"unit dir: {plan.unit_dir}")
    lines.append("")

    verb = "wrote" if applied else "would write"
    lines.append(f"{verb}:")
    for unit in plan.units:
        lines.append(f"  {plan.unit_dir / unit.name}")

    verb = "ran" if applied else "would run"
    lines.append(f"{verb}:")
    for command in plan.commands:
        lines.append(f"  {' '.join(command)}")
    lines.append("")

    if plan.linger_already_enabled:
        lines.append(f"lingering: already enabled for {plan.linger_user}")
    else:
        verb = "enabled" if applied else "would enable"
        lines.append(
            f"lingering: {verb} for {plan.linger_user} — without it these units "
            "would stop at logout instead of running from boot"
        )
    lines.append(f"note: {_RULE_NOTE}")
    lines.append("")

    if show_units:
        for unit in plan.units:
            lines.append(f"--- {plan.unit_dir / unit.name} ---")
            lines.append(unit.content.rstrip("\n"))
            lines.append("")

    if applied:
        lines.append("applied: yes")
        lines.append("next: sensibo service status")
    else:
        lines.append("applied: no (dry-run — pass --apply to commit)")
        if not show_units:
            lines.append("tip: --show-units prints the full unit files this would write")
    return "\n".join(lines)


def cmd_install(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    apply = bool(getattr(args, "apply", False))

    try:
        if apply:
            require_systemd()
        exec_path = resolve_exec_path(getattr(args, "exec_path", None))
        plan = build_install_plan(
            exec_path=exec_path,
            unit_dir=_unit_dir(args),
            collect=not args.no_collect,
            web=not args.no_web,
            interval=args.interval,
            bind=args.bind,
            db=args.db,
            token_file=args.token_file,
            runner=_runner(),
        )
        outcome = apply_install(plan, runner=_runner()) if apply else None
    except ServiceError as err:
        raise _as_cli_error(err) from None

    if json_mode:
        payload = plan.to_dict()
        payload["apply"] = apply
        payload["result"] = outcome
        payload["rule_daemon"] = _RULE_NOTE
        payload[EXECUTION_FIELD] = EXECUTION_SUPERVISED
        emit_result(payload, json_mode=True)
        return 0

    emit_result(
        _render_install_text(plan, applied=apply, show_units=bool(args.show_units)),
        json_mode=False,
    )
    return 0


# -- uninstall --------------------------------------------------------------


def _render_uninstall_text(plan: dict[str, object], *, applied: bool) -> str:
    remove = list(plan["remove"])  # type: ignore[call-overload]
    lines = ["sensibo service uninstall", _EXECUTION_LINE, ""]
    lines.append(f"unit dir: {plan['unit_dir']}")
    lines.append("")

    if not remove:
        lines.append("nothing to remove: no sensibo units are installed")
        return "\n".join(lines)

    verb = "removed" if applied else "would remove"
    lines.append(f"{verb}:")
    for name in remove:
        lines.append(f"  {Path(str(plan['unit_dir'])) / str(name)}")

    verb = "ran" if applied else "would run"
    lines.append(f"{verb}:")
    for command in plan["commands"]:  # type: ignore[union-attr]
        lines.append(f"  {' '.join(command)}")
    lines.append("")
    lines.append(f"lingering: {plan['linger']}")
    lines.append("")
    lines.append("applied: yes" if applied else "applied: no (dry-run — pass --apply to commit)")
    return "\n".join(lines)


def cmd_uninstall(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    apply = bool(getattr(args, "apply", False))

    try:
        if apply:
            require_systemd()
        plan = build_uninstall_plan(unit_dir=_unit_dir(args))
        outcome = apply_uninstall(plan, runner=_runner()) if apply else None
    except ServiceError as err:
        raise _as_cli_error(err) from None

    if json_mode:
        payload: dict[str, object] = {
            "unit_dir": str(plan["unit_dir"]),
            "remove": list(plan["remove"]),  # type: ignore[call-overload]
            "commands": [list(c) for c in plan["commands"]],  # type: ignore[union-attr]
            "linger": plan["linger"],
            "apply": apply,
            "result": outcome,
            EXECUTION_FIELD: EXECUTION_SUPERVISED,
        }
        emit_result(payload, json_mode=True)
        return 0

    emit_result(_render_uninstall_text(plan, applied=apply), json_mode=False)
    return 0


# -- status -----------------------------------------------------------------


def _store_freshness(db: str | None) -> dict[str, object]:
    """How recently did a reading actually land? The real 'is collection alive' answer.

    Deliberately does not create the store: a status probe that materialises an
    empty database would report "healthy, 0 locations" for a machine where
    ``collect`` has never run — a lie by side effect.
    """
    path = Path(db).expanduser() if db else default_db_path()
    if not path.is_file():
        return {"db": str(path), "exists": False, "locations": 0, "newest_age_seconds": None}

    newest: float | None = None
    with Store(db_path=path) as store:
        locations = store.list_locations()
        for location in locations:
            for reading in store.latest_readings(location.id).values():
                if newest is None or reading.timestamp > newest:
                    newest = reading.timestamp

    age = None if newest is None else max(0.0, time.time() - newest)
    return {
        "db": str(path),
        "exists": True,
        "locations": len(locations),
        "newest_age_seconds": age,
    }


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


def _render_status_text(state: dict[str, object], store: dict[str, object]) -> str:
    lines = ["sensibo service status", _EXECUTION_LINE, ""]
    lines.append(f"unit dir: {state['unit_dir']}")

    if state["linger"]:
        lines.append(
            f"linger:   enabled for {state['user']} (units start at boot, no login needed)"
        )
    else:
        lines.append(
            f"linger:   DISABLED for {state['user']} — these units stop at logout and "
            "do NOT start at boot"
        )
    lines.append("")

    if not state["installed"]:
        lines.append("units:    not installed")
        lines.append("hint:     run 'sensibo service install' to see the plan, then --apply")
    else:
        width = max(len(name) for name in ALL_UNITS)
        for unit in state["units"]:  # type: ignore[union-attr]
            name = str(unit["unit"])
            if not unit["installed"]:
                lines.append(f"  {name:<{width}}  not installed")
                continue
            lines.append(f"  {name:<{width}}  {unit['enabled']:<9} {unit['active']}")
    lines.append("")

    # The store section prints unconditionally, installed or not. "You already
    # have readings and nothing is keeping them fresh" is precisely the state an
    # operator who has not run `install` is in — hiding it behind the units
    # being installed would withhold the one number that shows the problem.
    lines.append(f"store: {store['db']}")
    if not store["exists"]:
        lines.append("  no store yet — collect has never written a reading")
    else:
        age = _format_age(store["newest_age_seconds"])  # type: ignore[arg-type]
        lines.append(f"  {store['locations']} location(s), newest reading {age}")
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))

    try:
        require_systemd()
        state = service_status(unit_dir=_unit_dir(args), runner=_runner())
    except ServiceError as err:
        raise _as_cli_error(err) from None

    store = _store_freshness(getattr(args, "db", None))

    if json_mode:
        payload = dict(state)
        payload["store"] = store
        payload[EXECUTION_FIELD] = EXECUTION_SUPERVISED
        emit_result(payload, json_mode=True)
        return 0

    emit_result(_render_status_text(state, store), json_mode=False)
    return 0


# -- overview / registration ------------------------------------------------


def _service_sections() -> list[dict[str, object]]:
    return [
        {
            "title": "Verbs",
            "items": [
                "service install [--apply] — write the systemd user units, enable "
                "lingering, start them (dry-run by default)",
                "service status — per-unit enabled/active, the lingering flag, and "
                "how recently a reading actually landed",
                "service uninstall [--apply] — disable, stop, and delete the units",
                "service overview — describe this noun (this command)",
            ],
        },
        {
            "title": "What gets installed",
            "items": [
                "sensibo-collect.service — 'collect --daemon', Restart=always",
                "sensibo-web.service — 'web', Restart=always",
                "sensibo.target — groups both, WantedBy=default.target",
                "loginctl enable-linger — starts the user manager at BOOT, not at login",
            ],
        },
        {
            "title": "Why it matters",
            "items": [
                "Sensibo's cloud serves only ~7 days of history, so a collection gap "
                "while the host is asleep is permanently lost data",
                "'collect --daemon' exits on an ApiError; Restart=always is the only "
                "thing that makes it survive a cloud blip or a boot-time network race",
                _RULE_NOTE,
            ],
        },
        {"title": "Execution", "items": [_EXECUTION_LINE]},
    ]


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "sensibo service",
        _service_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_overview(args)


def _add_unit_dir(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--unit-dir",
        dest="unit_dir",
        default=None,
        metavar="PATH",
        help="Override the systemd user unit directory (default: ~/.config/systemd/user).",
    )


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "service",
        help="Keep collection and the dashboard always-on under systemd "
        "(see 'sensibo service overview').",
    )
    p.add_argument("--json", action="store_true", help=JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="service_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the service noun.")
    ov.add_argument("--json", action="store_true", help=JSON_HELP)
    ov.set_defaults(func=cmd_overview)

    install = noun_sub.add_parser(
        "install",
        help="Write + enable the systemd user units (dry-run by default; --apply commits).",
    )
    install.add_argument(
        "--apply",
        action="store_true",
        help="Commit: write the units, enable lingering, and start them.",
    )
    install.add_argument(
        "--interval",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="Collector poll interval baked into the unit (default: 60).",
    )
    install.add_argument(
        "--bind",
        default="0.0.0.0:8323",
        metavar="ADDR:PORT",
        help="Dashboard bind address baked into the unit (default: 0.0.0.0:8323).",
    )
    install.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Store path for both units (default: $SENSIBO_DB or ~/.sensibo/sensibo.db).",
    )
    install.add_argument(
        "--token-file",
        dest="token_file",
        default=None,
        metavar="PATH",
        help="Dashboard write-auth token path (default: ~/.sensibo/web-token).",
    )
    install.add_argument(
        "--exec-path",
        dest="exec_path",
        default=None,
        metavar="PATH",
        help="Absolute path of the 'sensibo' console script (default: resolved from PATH).",
    )
    install.add_argument(
        "--no-collect",
        dest="no_collect",
        action="store_true",
        help="Do not install the collector unit.",
    )
    install.add_argument(
        "--no-web",
        dest="no_web",
        action="store_true",
        help="Do not install the dashboard unit.",
    )
    install.add_argument(
        "--show-units",
        dest="show_units",
        action="store_true",
        help="Print the full unit file contents the plan would write.",
    )
    _add_unit_dir(install)
    install.add_argument("--json", action="store_true", help=JSON_HELP)
    install.set_defaults(func=cmd_install)

    uninstall = noun_sub.add_parser(
        "uninstall",
        help="Disable, stop, and delete the units (dry-run by default; --apply commits).",
    )
    uninstall.add_argument(
        "--apply",
        action="store_true",
        help="Commit: disable, stop, and delete the units.",
    )
    _add_unit_dir(uninstall)
    uninstall.add_argument("--json", action="store_true", help=JSON_HELP)
    uninstall.set_defaults(func=cmd_uninstall)

    st = noun_sub.add_parser(
        "status",
        help="Per-unit enabled/active state, the lingering flag, and store freshness.",
    )
    st.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Store to check freshness against (default: $SENSIBO_DB or ~/.sensibo/sensibo.db).",
    )
    _add_unit_dir(st)
    st.add_argument("--json", action="store_true", help=JSON_HELP)
    st.set_defaults(func=cmd_status)
