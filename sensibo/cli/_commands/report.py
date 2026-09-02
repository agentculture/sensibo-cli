"""``sensibo report`` — render (and optionally deliver) an offline SVG report (task t7).

Wraps :func:`sensibo.report.render_report` with the same dry-run-by-default
shape every write verb in this project uses:

* with neither ``--out`` nor ``--apply``, prints where the report *would* be
  written (``~/.sensibo/reports`` by default, or ``$SENSIBO_REPORTS_DIR``) and
  the redacted transport(s) it would notify, and writes nothing;
* ``--out PATH`` writes the rendered SVG to ``PATH`` — writing a file the
  operator explicitly asked for is fine without ``--apply``, same as any other
  verb that takes an explicit output path;
* ``--apply`` writes the report into the reports directory and delivers a
  notification (never a file upload — just a small JSON message naming the
  path) through the configured transport(s), **exactly once**. ``delivered``
  is derived from the outcomes (any transport ``ok``); the last-sent meta key
  (so the in-daemon scheduler in ``sensibo collect --daemon`` treats this as
  satisfying that kind's next-due check) is recorded only when delivery
  actually succeeded on at least one configured transport, or none is
  configured at all (the file on disk is then itself the deliverable). A
  configured transport that failed on every leg raises :class:`CliError`
  (exit 2) instead of reporting success, so the next attempt still finds the
  report due (Qodo review Q5).

Every response carries ``execution: local (stops when this daemon stops)``:
like ``notify test``, this only ever runs on demand or while ``sensibo
collect --daemon`` is running — never in Sensibo's own cloud.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from sensibo.cli._commands._automation import JSON_HELP
from sensibo.cli._commands.overview import emit_overview
from sensibo.cli._errors import EXIT_ENV_ERROR, CliError
from sensibo.cli._output import emit_result
from sensibo.health import EXECUTION_LOCAL
from sensibo.notify import Outcome, redact, render_dry_run, resolve_notify_config
from sensibo.report import (
    DAILY,
    META_LAST_DAILY,
    META_LAST_WEEKLY,
    WEEKLY,
    WINDOW_HOURS,
    build_payload,
    deliver_report,
    render_report,
    report_filename,
    reports_dir,
    resolve_dashboard_url,
    write_report,
)
from sensibo.store import Store

EXECUTION_FIELD = "execution"

_KIND_TO_META = {DAILY: META_LAST_DAILY, WEEKLY: META_LAST_WEEKLY}


def _configured_transports(config) -> list[dict[str, str]]:
    transports: list[dict[str, str]] = []
    if config.webhook_url:
        transports.append({"transport": "webhook", "target": redact(config.webhook_url, config)})
    if config.script_path:
        transports.append({"transport": "script", "target": config.script_path})
    return transports


def _outcomes_to_list(outcomes: list[Outcome]) -> list[dict[str, object]]:
    return [{"transport": o.transport, "ok": o.ok, "detail": o.detail} for o in outcomes]


def cmd_report(args: argparse.Namespace, *, kind: str) -> int:
    json_mode = bool(getattr(args, "json", False))
    apply = bool(getattr(args, "apply", False))
    out = getattr(args, "out", None)
    window_hours = WINDOW_HOURS[kind]

    with Store(db_path=getattr(args, "db", None)) as store:
        now = time.time()
        svg = render_report(store, window_hours, now=now)

        written_to: str | None = None
        if out:
            out_path = Path(out)
            out_path.write_text(svg, encoding="utf-8")
            written_to = str(out_path)

        config = resolve_notify_config()
        target_dir = reports_dir()
        transports = _configured_transports(config)

        delivered = False
        outcomes: list[dict[str, object]] = []

        if apply:
            path = write_report(kind, svg, now, target_dir)
            raw_outcomes = deliver_report(kind, path, config, resolve_dashboard_url())
            outcomes = _outcomes_to_list(raw_outcomes)
            delivered = any(bool(outcome["ok"]) for outcome in outcomes)
            written_to = str(path)

            # Q5: a configured transport that failed on every leg must not
            # be reported as success, and must not advance the scheduling
            # meta (so a retry, e.g. via `sensibo report daily --apply`, is
            # still due). No transport configured at all is not this case —
            # the file on disk written above is itself the deliverable, same
            # rule as the daemon's run_due_reports (Q4).
            if config.configured and not delivered:
                failed = ", ".join(
                    f"{outcome['transport']} ({outcome['detail']})"
                    for outcome in outcomes
                    if not outcome["ok"]
                )
                raise CliError(
                    code=EXIT_ENV_ERROR,
                    message=f"report delivery failed on: {failed}",
                    remediation=(
                        "check SENSIBO_NOTIFY_WEBHOOK / SENSIBO_NOTIFY_SCRIPT and run "
                        "sensibo notify test --apply"
                    ),
                )

            store.set_meta(_KIND_TO_META[kind], repr(now))
        else:
            would_path = target_dir / report_filename(kind, now)
            payload = build_payload(kind, would_path, resolve_dashboard_url())

        result = {
            "kind": kind,
            "window_hours": window_hours,
            "apply": apply,
            "written_to": written_to,
            "delivered": delivered,
            "transports": transports,
            "outcomes": outcomes,
            EXECUTION_FIELD: EXECUTION_LOCAL,
        }

        if json_mode:
            emit_result(result, json_mode=True)
            return 0

        lines = [f"report {kind}: window={window_hours}h"]
        if apply:
            lines.append(f"  written to: {written_to}")
            lines.append(f"  delivered to {len(outcomes)} transport(s):")
            for outcome in outcomes:
                mark = "ok" if outcome["ok"] else "FAILED"
                lines.append(f"    {outcome['transport']}: {mark} ({outcome['detail']})")
        else:
            if written_to:
                lines.append(f"  written to: {written_to}")
            lines.append(f"  would write to: {would_path}")
            lines.append(render_dry_run(payload, config))
        lines.append(f"  {EXECUTION_FIELD}: {EXECUTION_LOCAL}")
        emit_result("\n".join(lines), json_mode=False)
        return 0


def cmd_report_daily(args: argparse.Namespace) -> int:
    return cmd_report(args, kind=DAILY)


def cmd_report_weekly(args: argparse.Namespace) -> int:
    return cmd_report(args, kind=WEEKLY)


def _report_sections() -> list[dict[str, object]]:
    return [
        {
            "title": "Verbs",
            "items": [
                "daily [--out PATH] [--apply] — render (and optionally deliver) the last 24h",
                "weekly [--out PATH] [--apply] — render (and optionally deliver) the last 7d",
                "overview — describe this noun",
            ],
        },
        {"title": "Execution", "items": [f"{EXECUTION_FIELD}: {EXECUTION_LOCAL}"]},
    ]


def cmd_report_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "sensibo report",
        _report_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    # `sensibo report` with no sub-verb prints the noun's overview.
    return cmd_report_overview(args)


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--out",
        help="Also write the rendered SVG to this path (allowed without --apply).",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Write into the reports directory, record it as sent, and deliver a "
            "notification (default: dry-run preview only, nothing written or sent)."
        ),
    )
    p.add_argument(
        "--db",
        help="Path to the local sqlite store (default: $SENSIBO_DB or ~/.sensibo/sensibo.db).",
    )
    p.add_argument("--json", action="store_true", help=JSON_HELP)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "report",
        help=(
            "Render (and optionally deliver) an offline SVG report "
            "(see 'sensibo report overview')."
        ),
    )
    p.add_argument("--json", action="store_true", help=JSON_HELP)
    p.set_defaults(func=_no_verb)
    # Propagate the structured-error parser class so every sub-verb's parse
    # errors route through the CliError contract, not argparse's default.
    report_sub = p.add_subparsers(dest="report_command", parser_class=type(p))

    ov = report_sub.add_parser("overview", help="Describe the report noun.")
    ov.add_argument("--json", action="store_true", help=JSON_HELP)
    ov.set_defaults(func=cmd_report_overview)

    daily_p = report_sub.add_parser(
        "daily", help="Render (and optionally deliver) the trailing 24h report."
    )
    _add_common_args(daily_p)
    daily_p.set_defaults(func=cmd_report_daily)

    weekly_p = report_sub.add_parser(
        "weekly", help="Render (and optionally deliver) the trailing 7-day report."
    )
    _add_common_args(weekly_p)
    weekly_p.set_defaults(func=cmd_report_weekly)
