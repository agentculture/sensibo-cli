"""One SVG report document for every (location, numeric field) pair.

Task t4. A single self-contained ``<svg>`` traced directly from the local
store: a title line naming the window and the generation time (ISO 8601 UTC),
then one panel per location and numeric field, stacked vertically at a fixed
document width. Kept in its own module so it is unit-testable without any
HTTP server or network access (see ``tests/test_report_chart.py``).

**Bounded point count.** Each series is drawn from
:meth:`sensibo.store.Store.query_range` downsampled evenly to at most
``max_points`` via :func:`sensibo.web._svg._downsample` — the same helper the
web dashboard's sparklines use, which always keeps the first and last reading
so a panel's visible start/end values never silently shift.

A location with no readings in the window renders a labelled empty panel
("no readings in window") rather than raising. All text is escaped with
:func:`html.escape`; there are no ``<script>`` elements.
"""

from __future__ import annotations

import datetime
import html as _html
import time

from sensibo.store import LocationRecord, ReadingRecord, Store
from sensibo.web._svg import _downsample, _is_numeric

#: Fixed document width; panels stack vertically inside it.
DOCUMENT_WIDTH = 900

#: Default cap on how many points a single panel plots (overridable via
#: ``render_report(max_points=...)``).
DEFAULT_MAX_POINTS = 400

_TITLE_HEIGHT = 40
_PANEL_HEIGHT = 120
_PANEL_PAD = 10


def _window_label(window_hours: int) -> str:
    if window_hours == 24:
        return "Last 24 hours"
    if window_hours % 24 == 0:
        days = window_hours // 24
        return f"Last {days} day{'s' if days != 1 else ''}"
    return f"Last {window_hours} hours"


def _location_label(location: LocationRecord) -> str:
    """The operator alias, else the Sensibo room name, else the stable id."""
    return location.alias or location.room_name or location.id


def _panel_svg(
    label: str,
    field: str,
    unit: str | None,
    readings: list[ReadingRecord],
    y: int,
) -> str:
    """One panel's SVG fragment at vertical offset ``y``.

    ``readings`` is already bounded by the caller (:func:`_downsample`). An
    empty list renders the labelled "no readings in window" placeholder.
    """
    caption = f"{label} — {field}" + (f" ({unit})" if unit else "")
    x = _PANEL_PAD
    head_y = y + 18
    parts = [
        f'<text x="{x}" y="{head_y}" font-size="14" font-weight="bold" '
        f'fill="currentColor">{_html.escape(caption)}</text>'
    ]
    if not readings:
        parts.append(
            f'<text x="{x}" y="{y + _PANEL_HEIGHT - 12}" font-size="12" '
            f'fill="currentColor">no readings in window</text>'
        )
        return "".join(parts)

    values = [float(r.value) for r in readings]
    lo, hi = min(values), max(values)
    latest = values[-1]
    stats = f"min {lo:g} / max {hi:g} / latest {latest:g}"
    parts.append(
        f'<text x="{x}" y="{y + _PANEL_HEIGHT - 12}" font-size="12" '
        f'fill="currentColor">{_html.escape(stats)}</text>'
    )

    span = (hi - lo) or 1.0  # a flat series still renders a flat mid-height line
    n = len(values)
    inner_w = DOCUMENT_WIDTH - 2 * _PANEL_PAD
    inner_h = _PANEL_HEIGHT - 50  # leave room for the caption and stats lines
    top = y + 30
    points: list[str] = []
    for i, value in enumerate(values):
        px = _PANEL_PAD + (inner_w * i / (n - 1) if n > 1 else inner_w / 2)
        py = top + inner_h * (1 - (value - lo) / span)
        points.append(f"{px:.1f},{py:.1f}")
    parts.append(
        f'<polyline points="{" ".join(points)}" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />'
    )
    return "".join(parts)


def render_report(
    store: Store,
    window_hours: int,
    now: float | None = None,
    max_points: int = DEFAULT_MAX_POINTS,
) -> str:
    """Render one SVG report covering the trailing ``window_hours`` window.

    One panel per (location, numeric field) pair found via
    :meth:`Store.list_locations` and :meth:`Store.latest_readings` — non-numeric
    fields (e.g. a mode string) are skipped. Each panel draws
    :meth:`Store.query_range` over the window, downsampled evenly to at most
    ``max_points`` keeping the first and last reading. The title line names the
    window ("Last 24 hours" / "Last 7 days" / ...) and the generation time in
    ISO 8601 UTC. ``now`` defaults to the real current time; tests pass a
    fixed reference instant for determinism.
    """
    reference = time.time() if now is None else float(now)
    since = reference - window_hours * 3600
    generated = datetime.datetime.fromtimestamp(reference, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    panels: list[tuple[LocationRecord, str, str | None, list[ReadingRecord]]] = []
    for location in store.list_locations():
        for field, latest in store.latest_readings(location.id).items():
            if not _is_numeric(latest.value):
                continue
            readings = [
                r
                for r in store.query_range(location.id, field, since=since, until=reference)
                if _is_numeric(r.value)
            ]
            panels.append((location, field, latest.unit, _downsample(readings, max_points)))

    height = _TITLE_HEIGHT + len(panels) * _PANEL_HEIGHT
    body: list[str] = [
        f'<text x="{_PANEL_PAD}" y="24" font-size="18" font-weight="bold" '
        f'fill="currentColor">{_html.escape(_window_label(window_hours))} — '
        f"generated {_html.escape(generated)}</text>"
    ]
    for index, (location, field, unit, readings) in enumerate(panels):
        y = _TITLE_HEIGHT + index * _PANEL_HEIGHT
        body.append(_panel_svg(_location_label(location), field, unit, readings, y))
    return (
        f'<svg width="{DOCUMENT_WIDTH}" height="{height}" viewBox="0 0 {DOCUMENT_WIDTH} {height}" '
        f'xmlns="http://www.w3.org/2000/svg">' + "".join(body) + "</svg>"
    )
