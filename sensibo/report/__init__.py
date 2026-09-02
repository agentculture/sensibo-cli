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
"""

from __future__ import annotations

from .chart import DEFAULT_MAX_POINTS, render_report

__all__ = [
    "DEFAULT_MAX_POINTS",
    "render_report",
]
