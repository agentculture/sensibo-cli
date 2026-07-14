"""Server-side inline SVG sparklines for the web dashboard (task t12).

No external assets, no CDN, no JavaScript charting library: a single
``<svg>``/``<polyline>`` traced directly from a list of
:class:`sensibo.store.ReadingRecord`. Kept in its own module so it is
unit-testable without spinning an HTTP server (see ``tests/test_web_svg.py``).

**Bounded point count (Qodo review 3581287838).** Rendering one ``<svg>``
point per reading is fine for a handful of samples and explodes after months
of ~90s-cadence collection — a single location page could otherwise trace a
polyline with tens of thousands of points. :func:`render_sparkline` caps
itself at :data:`DEFAULT_MAX_POINTS` points by default (overridable via
``max_points``), downsampling evenly via :func:`_downsample` when the series
is larger. Downsampling always keeps the first and last reading, so a
sparkline's visible start/end values never silently shift.
"""

from __future__ import annotations

import html as _html

from sensibo.store import ReadingRecord

DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 80
_PAD = 6

#: Default cap on how many points a single sparkline ever plots. Chosen well
#: above the sparkline's own pixel width (there is no visual benefit to more
#: points than that), and small enough that even a store holding years of
#: history renders a bounded-size SVG.
DEFAULT_MAX_POINTS = 300


def _is_numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _downsample(readings: list[ReadingRecord], max_points: int) -> list[ReadingRecord]:
    """Evenly reduce ``readings`` to at most ``max_points`` entries.

    Deterministic index-based sampling (not time-bucket averaging) — cheap,
    dependency-free, and it always keeps the first and last reading so a
    sparkline's visible start/end values never silently shift under
    downsampling. A no-op when ``readings`` is already at or under the cap,
    so the common case (a location with only a handful of readings) never
    pays for it.
    """
    n = len(readings)
    if max_points <= 0 or n <= max_points:
        return readings
    if max_points == 1:
        return [readings[-1]]
    step = (n - 1) / (max_points - 1)
    seen: set[int] = set()
    sampled: list[ReadingRecord] = []
    for i in range(max_points):
        idx = round(i * step)
        if idx in seen:
            continue
        seen.add(idx)
        sampled.append(readings[idx])
    return sampled


def _placeholder(width: int, height: int, label: str) -> str:
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{_html.escape(label)}" xmlns="http://www.w3.org/2000/svg">'
        f'<text x="{_PAD}" y="{height // 2}" font-size="12" fill="currentColor">'
        f"{_html.escape(label)}</text></svg>"
    )


def render_sparkline(
    readings: list[ReadingRecord],
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    max_points: int = DEFAULT_MAX_POINTS,
) -> str:
    """Render one field's history as an inline SVG polyline, oldest to newest.

    Non-numeric readings (e.g. a mode string) are dropped rather than
    crashing — the store deliberately keeps whatever fields a pod reports,
    numeric or not (``docs/sensibo-api.md``), and only numeric series are
    chartable. An empty or all-non-numeric series renders a small "no data"
    placeholder instead of an empty ``<svg>``.

    If more than ``max_points`` numeric readings remain, they are downsampled
    evenly (:func:`_downsample`) before plotting — the fix for Qodo review
    3581287838, so a location with months of history still renders a
    bounded-size chart instead of one point per reading ever collected.
    """
    numeric = [r for r in readings if _is_numeric(r.value)]
    if not numeric:
        return _placeholder(width, height, "no data")

    numeric = _downsample(numeric, max_points)
    values = [float(r.value) for r in numeric]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0  # a flat series still renders a flat mid-height line
    n = len(values)
    inner_w = width - 2 * _PAD
    inner_h = height - 2 * _PAD

    points: list[str] = []
    for i, value in enumerate(values):
        x = _PAD + (inner_w * i / (n - 1) if n > 1 else inner_w / 2)
        y = _PAD + inner_h * (1 - (value - lo) / span)
        points.append(f"{x:.1f},{y:.1f}")
    points_attr = " ".join(points)

    label = f"{values[0]:g} to {values[-1]:g} (min {lo:g}, max {hi:g}, n={n})"
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{_html.escape(label)}" xmlns="http://www.w3.org/2000/svg">'
        f'<polyline points="{points_attr}" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />'
        "</svg>"
    )
