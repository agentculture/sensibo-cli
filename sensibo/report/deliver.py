"""Report files-on-disk and delivery: where reports live, and telling someone (task t7).

Two concerns kept apart:

* **Where reports land** — :func:`reports_dir` (``SENSIBO_REPORTS_DIR``, else
  ``~/.sensibo/reports``, created 0700 on first write) and :func:`write_report`
  (the SVG document, named ``daily-YYYY-MM-DD.svg`` / ``weekly-YYYY-Www.svg``).
* **Telling someone** — :func:`deliver_report` builds a
  :class:`~sensibo.notify.Payload` (``kind="report"``) naming the file and,
  when ``SENSIBO_DASHBOARD_URL`` is configured, its dashboard link, then hands
  it to either an injected ``notifier`` callable (the daemon's
  ``Notifier = Callable[[Payload], Sequence[Outcome]]`` shape — see
  :mod:`sensibo.collect.collector`) or, absent one, straight to
  :func:`sensibo.notify.send` with the resolved config. Never a multipart file
  upload — the payload only ever carries the *path*, matching every other
  notification this project sends.

:func:`run_due_reports` is the glue the daemon loop calls after every cycle:
ask :func:`sensibo.report.schedule.due_reports` what's due, render+write+
deliver each, then set the last-sent meta — but only once delivery is known
good (no transport configured, or at least one transport reported ``ok``).
It never raises — a scheduling misconfig or a delivery hiccup here must not
take down collection; a malformed last-sent meta value is treated as "never
sent" rather than aborting the cycle, and on any per-kind render/write
failure or total delivery failure the meta key is left unset so the same
kind is retried next cycle.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sensibo.notify import NotifyConfig, Payload, redact, send
from sensibo.report.chart import render_report
from sensibo.report.schedule import (
    DAILY,
    META_LAST_DAILY,
    META_LAST_WEEKLY,
    WEEKLY,
    ReportSchedule,
    due_reports,
)
from sensibo.store import Store

#: Overrides where reports are written; default mirrors ``sensibo.store``'s
#: own ``~/.sensibo``-relative convention.
REPORTS_DIR_VAR = "SENSIBO_REPORTS_DIR"
#: Base URL of an operator-run dashboard; when set, delivery messages carry a
#: direct link to the written report.
DASHBOARD_URL_VAR = "SENSIBO_DASHBOARD_URL"

_DEFAULT_RELATIVE = Path(".sensibo") / "reports"

#: Window each kind renders — matches ``sensibo report daily|weekly``.
WINDOW_HOURS = {DAILY: 24, WEEKLY: 168}

#: A notify transport: takes a payload, returns one outcome per transport.
#: Same shape as ``sensibo.collect.collector.Notifier`` — injectable so the
#: daemon hook and tests never resolve a real webhook.
Notifier = Callable[[Payload], Sequence[Any]]

#: A diagnostics logger: one line, no return value. Defaults to a no-op so
#: library callers never need to supply one.
Logger = Callable[[str], None]


def _noop(_message: str) -> None:  # pragma: no cover - trivial default
    return None


def reports_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve where reports are written: ``SENSIBO_REPORTS_DIR``, else ``~/.sensibo/reports``.

    Mirrors :func:`sensibo.store.resolve_db_path`'s precedence. Never touches
    the filesystem — directory creation happens in :func:`write_report`.
    """
    environ = env if env is not None else os.environ
    override = environ.get(REPORTS_DIR_VAR)
    if override:
        return Path(override)
    return Path.home() / _DEFAULT_RELATIVE


def resolve_dashboard_url(env: Mapping[str, str] | None = None) -> str | None:
    """The configured dashboard base URL, or ``None`` if unset/empty."""
    environ = env if env is not None else os.environ
    return environ.get(DASHBOARD_URL_VAR) or None


def _ensure_dir(path: Path) -> None:
    """Create ``path`` with restrictive (0700) permissions if it doesn't exist."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def report_filename(kind: str, now: float) -> str:
    """The filename a report of ``kind`` generated at ``now`` is written under.

    ``daily-YYYY-MM-DD.svg`` (UTC calendar day) or ``weekly-YYYY-Www.svg``
    (ISO week number, UTC).
    """
    moment = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
    if kind == DAILY:
        return f"daily-{moment.strftime('%Y-%m-%d')}.svg"
    if kind == WEEKLY:
        iso_year, iso_week, _ = moment.isocalendar()
        return f"weekly-{iso_year:04d}-W{iso_week:02d}.svg"
    raise ValueError(f"unknown report kind: {kind!r} (expected {DAILY!r} or {WEEKLY!r})")


def write_report(kind: str, svg: str, now: float, reports_dir: Path) -> Path:
    """Write ``svg`` under ``reports_dir`` and return the path written.

    Creates ``reports_dir`` (mode 0700) if it doesn't already exist.
    """
    _ensure_dir(reports_dir)
    path = reports_dir / report_filename(kind, now)
    path.write_text(svg, encoding="utf-8")
    return path


def build_payload(kind: str, path: Path, dashboard_url: str | None) -> Payload:
    """The :class:`~sensibo.notify.Payload` naming ``path``, and a dashboard link if known.

    Carries only the file path — never the SVG content — so delivery is
    always a small JSON message, never a multipart upload.
    """
    message = f"{kind} report written to {path}"
    if dashboard_url:
        link = f"{dashboard_url.rstrip('/')}/reports/{path.name}"
        message = f"{message} ({link})"
    return Payload(
        kind="report",
        location=None,  # type: ignore[arg-type]
        status=kind,
        since=None,  # type: ignore[arg-type]
        last_ok=None,  # type: ignore[arg-type]
        message=message,
    )


def deliver_report(
    kind: str,
    path: Path,
    config: NotifyConfig,
    dashboard_url: str | None,
    notifier: Notifier | None = None,
) -> list[Any]:
    """Deliver one report's notification; never a file upload.

    With ``notifier`` given, calls it with the built payload (the shape the
    daemon's :mod:`sensibo.collect` uses). Without one, calls
    :func:`sensibo.notify.send` directly with ``config`` — the shape
    ``sensibo report --apply`` uses.
    """
    payload = build_payload(kind, path, dashboard_url)
    if notifier is not None:
        return list(notifier(payload))
    return list(send(payload, config))


@dataclass(frozen=True)
class ReportRun:
    """One due report's render-write-deliver outcome, from :func:`run_due_reports`."""

    kind: str
    path: Path
    outcomes: list[Any]


def _parse_last_sent(
    raw: str | None, meta_key: str, config: NotifyConfig, logger: Logger
) -> float | None:
    """Parse one ``last_*_report_at`` meta value; malformed counts as "never sent".

    Never raises (Qodo review Q15): a store meta value can be corrupted by
    something outside this process's control, and one bad value must not
    abort scheduling for the *other* report kind. On a malformed value, logs
    one redacted diagnostic and returns ``None`` (which :func:`due_reports`
    treats as "never sent", i.e. always due).
    """
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError) as err:
        logger(redact(f"report: malformed {meta_key} meta {raw!r}: {err}", config))
        return None


def run_due_reports(
    store: Store,
    schedule: ReportSchedule,
    config: NotifyConfig,
    now: float,
    notifier: Notifier | None,
    reports_dir_path: Path,
    *,
    log: Logger | None = None,
) -> list[ReportRun]:
    """Render, write, and deliver every report due right now; used by the daemon hook.

    Reads ``last_daily_report_at``/``last_weekly_report_at`` from ``store``'s
    meta table, asks :func:`~sensibo.report.schedule.due_reports` which kinds
    are due, and for each one: render via
    :func:`~sensibo.report.chart.render_report`, write via
    :func:`write_report`, deliver via :func:`deliver_report`, then set the
    meta key — but **only** when (a) no transport is configured (the file on
    disk *is* the deliverable) or (b) at least one configured transport
    reported ``ok`` (Qodo review Q4). On total delivery failure the meta key
    is left unchanged, re-rendering to the same filename next cycle is
    idempotent, and one redacted diagnostic is logged.

    Never raises: any exception for one kind — including a malformed
    last-sent meta value (Q15) — is logged (via ``log``, default a no-op,
    with the configured webhook URL redacted per Q10) and that kind is
    simply skipped or retried next cycle. A bad report must never take down
    the collector daemon that called this.
    """
    logger = log or _noop

    # Q15: timestamp parsing lives inside the never-raises boundary — a
    # malformed value for one kind must not stop the other kind processing.
    last_daily_at = _parse_last_sent(
        store.get_meta(META_LAST_DAILY), META_LAST_DAILY, config, logger
    )
    last_weekly_at = _parse_last_sent(
        store.get_meta(META_LAST_WEEKLY), META_LAST_WEEKLY, config, logger
    )

    try:
        due = due_reports(schedule, now, last_daily_at, last_weekly_at)
    except Exception as err:  # noqa: BLE001 - must never propagate into the daemon loop
        logger(redact(f"report: schedule computation failed this cycle: {err}", config))
        return []

    dashboard_url = resolve_dashboard_url()

    runs: list[ReportRun] = []
    for kind in due:
        try:
            svg = render_report(store, WINDOW_HOURS[kind], now=now)
            path = write_report(kind, svg, now, reports_dir_path)
            outcomes = deliver_report(kind, path, config, dashboard_url, notifier=notifier)
        except Exception as err:  # noqa: BLE001 - must never propagate into the daemon loop
            logger(redact(f"report: {kind} report failed this cycle: {err}", config))
            continue

        # Q4: only persist "last sent" when the file itself is the
        # deliverable (no transport configured) or delivery actually
        # succeeded on at least one transport — otherwise leave the meta
        # unset so the same kind is retried (and re-rendered, idempotently)
        # next cycle.
        delivered_ok = any(getattr(outcome, "ok", False) for outcome in outcomes)
        if not config.configured or delivered_ok:
            meta_key = META_LAST_DAILY if kind == DAILY else META_LAST_WEEKLY
            store.set_meta(meta_key, repr(now))
        else:
            logger(
                redact(
                    f"report: {kind} report delivery failed on every configured "
                    "transport; will retry next cycle",
                    config,
                )
            )
        runs.append(ReportRun(kind=kind, path=path, outcomes=outcomes))
    return runs
