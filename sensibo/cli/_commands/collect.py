"""``sensibo collect`` — poll the fleet on a cadence and persist every reading.

This verb is the retention pillar's front door. It wires the cloud client to
the local store via :class:`sensibo.collect.Collector` and adds only the two
things a CLI owns and the engine does not: argument handling (the ``--once`` /
``--daemon`` mode split and the ``--interval`` floor) and the stdout/stderr
split (a machine-readable summary to stdout, human progress to stderr).

``collect`` **reads** from the cloud and **writes only to the local store** —
it never drives an AC — so unlike the control verbs it has no ``--apply`` gate.

Testing seams: :func:`build_client` (the real client factory) and
:func:`_sleep` (the daemon's inter-cycle wait) are module-level so a test can
monkeypatch a fake client and an instant/interrupting sleep, and ``--db`` points
the store at a ``tmp_path`` — no test ever touches the cloud or ``~/.sensibo``.
"""

from __future__ import annotations

import argparse
import time

from sensibo.api import ApiError, SensiboClient
from sensibo.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from sensibo.cli._output import emit_diagnostic, emit_result
from sensibo.collect import DEFAULT_INTERVAL, MIN_INTERVAL, Collector
from sensibo.store import Store


def build_client() -> SensiboClient:
    """Construct the real Sensibo client. Monkeypatched to a fake in tests."""
    return SensiboClient()


def _sleep(seconds: float) -> None:
    """Wait between daemon cycles. A seam tests replace to avoid real sleeping."""
    time.sleep(seconds)


def _stderr_log(message: str) -> None:
    emit_diagnostic(message)


def _as_cli_error(err: ApiError) -> CliError:
    """Map an :class:`~sensibo.api.ApiError` onto the CLI's error contract.

    A missing key, a network failure, or exhausted 429 retries are all
    environment/setup problems from the operator's side — exit code 2.
    """
    return CliError(code=EXIT_ENV_ERROR, message=err.message, remediation=err.remediation)


def _render_text(summary: dict[str, object]) -> str:
    lines = [
        "collect: cycle complete",
        f"  locations seen: {summary['locations_seen']} "
        f"({summary['pods']} pod(s), {summary['room_sensors']} room sensor(s))",
        f"  readings written: {summary['readings_written']}",
    ]
    backfill = summary.get("backfill")
    if isinstance(backfill, dict):
        window = backfill.get("window_days")
        window_text = f"days={window}" if window is not None else "none accessible"
        lines.append(
            f"  backfill: {window_text}, "
            f"{backfill.get('readings_written', 0)} reading(s) recovered"
        )
    lines.append(f"  db: {summary['db']}")
    return "\n".join(lines)


def _summary_dict(outcome, store: Store, *, cycle: int | None = None) -> dict[str, object]:
    summary = outcome.to_summary()
    if cycle is not None:
        summary["cycle"] = cycle
    summary["db"] = str(store.path)
    return summary


def _run_once(collector: Collector, store: Store, json_mode: bool) -> int:
    outcome = collector.collect_once()
    summary = _summary_dict(outcome, store)
    emit_result(summary if json_mode else _render_text(summary), json_mode=json_mode)
    return 0


def _run_daemon(collector: Collector, store: Store, interval: float, json_mode: bool) -> int:
    emit_diagnostic(f"collect: daemon started — polling every {interval:g}s (Ctrl-C to stop)")
    cycles = 0
    try:
        while True:
            outcome = collector.collect_once()
            cycles += 1
            summary = _summary_dict(outcome, store, cycle=cycles)
            emit_result(summary if json_mode else _render_text(summary), json_mode=json_mode)
            _sleep(interval)
    except KeyboardInterrupt:
        emit_diagnostic(f"collect: stopped cleanly after {cycles} cycle(s)")
        return 0


def cmd_collect(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))

    if args.once and args.daemon:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="--once and --daemon are mutually exclusive",
            remediation="pass exactly one: --once for a single cycle, --daemon to loop",
        )
    if args.interval < MIN_INTERVAL:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=(
                f"--interval must be at least {MIN_INTERVAL}s (Sensibo's safe polling "
                f"floor); got {args.interval:g}s"
            ),
            remediation=f"raise --interval to {MIN_INTERVAL} or more",
        )

    try:
        client = build_client()
    except ApiError as err:
        raise _as_cli_error(err) from None

    with Store(db_path=getattr(args, "db", None)) as store:
        collector = Collector(client, store, log=_stderr_log)
        try:
            if args.daemon:
                return _run_daemon(collector, store, float(args.interval), json_mode)
            return _run_once(collector, store, json_mode)
        except ApiError as err:
            raise _as_cli_error(err) from None


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "collect",
        help="Poll the fleet on a cadence and persist every reading into the local store.",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single collection cycle and exit (the default when no mode is given).",
    )
    p.add_argument(
        "--daemon",
        action="store_true",
        help="Loop forever, polling every --interval seconds, until interrupted.",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=(
            f"Daemon poll interval in seconds (default {DEFAULT_INTERVAL}, hard floor "
            f"{MIN_INTERVAL})."
        ),
    )
    p.add_argument(
        "--db",
        help="Path to the local sqlite store (default: $SENSIBO_DB or ~/.sensibo/sensibo.db).",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_collect)
