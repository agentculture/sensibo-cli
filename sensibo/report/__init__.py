"""``sensibo.report`` — offline multi-location SVG reports (task t4).

Renders one self-contained ``<svg>`` document straight from the local sqlite
store (:mod:`sensibo.store`) — no network access, no external assets, no
JavaScript. Every numeric series is downsampled to a bounded point count
(reusing :func:`sensibo.web._svg._downsample`), so even years of ~90s-cadence
history render a bounded-size report.

Zero runtime dependencies: ``html``, ``datetime``, and ``time`` only (stdlib).

Public surface
--------------

* :func:`render_report` — one SVG document with a title line and one panel per
  (location, numeric field) pair.
* :class:`ReportSchedule` / :func:`due_reports` — task t7: when a daily/weekly
  report is due, from a schedule and the last-sent instant.
* :func:`reports_dir` / :func:`write_report` / :func:`deliver_report` /
  :func:`run_due_reports` — task t7: where reports live on disk and how they
  get delivered (a small notification naming the file, never a file upload).
"""

from __future__ import annotations

from .chart import DEFAULT_MAX_POINTS, render_report
from .deliver import (
    DASHBOARD_URL_VAR,
    REPORTS_DIR_VAR,
    WINDOW_HOURS,
    ReportRun,
    build_payload,
    deliver_report,
    report_filename,
    reports_dir,
    resolve_dashboard_url,
    run_due_reports,
    write_report,
)
from .schedule import (
    DAILY,
    DAILY_AT_VAR,
    META_LAST_DAILY,
    META_LAST_WEEKLY,
    WEEKLY,
    WEEKLY_AT_VAR,
    WEEKLY_DAY_VAR,
    ReportSchedule,
    due_reports,
)

__all__ = [
    "DAILY",
    "DAILY_AT_VAR",
    "DASHBOARD_URL_VAR",
    "DEFAULT_MAX_POINTS",
    "META_LAST_DAILY",
    "META_LAST_WEEKLY",
    "REPORTS_DIR_VAR",
    "ReportRun",
    "ReportSchedule",
    "WEEKLY",
    "WEEKLY_AT_VAR",
    "WEEKLY_DAY_VAR",
    "WINDOW_HOURS",
    "build_payload",
    "deliver_report",
    "due_reports",
    "render_report",
    "report_filename",
    "reports_dir",
    "resolve_dashboard_url",
    "run_due_reports",
    "write_report",
]
