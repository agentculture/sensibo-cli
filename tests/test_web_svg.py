"""Tests for sensibo.web._svg — server-side inline SVG sparklines (task t12).

No external assets, no CDN, no JS charting library — a `<polyline>` traced
directly from `ReadingRecord`s. Written first (TDD).
"""

from __future__ import annotations

import re

from sensibo.store import ReadingRecord
from sensibo.web._svg import render_sparkline


def _reading(value, ts: float) -> ReadingRecord:
    return ReadingRecord(
        location_id="pod-1", field="temperature", timestamp=ts, value=value, unit="C"
    )


def _points(svg: str) -> list[str]:
    match = re.search(r'points="([^"]*)"', svg)
    assert match is not None, svg
    return match.group(1).split()


def test_empty_series_renders_a_placeholder_not_a_polyline() -> None:
    svg = render_sparkline([])
    assert "<svg" in svg
    assert "no data" in svg
    assert "<polyline" not in svg


def test_series_renders_one_coordinate_pair_per_reading() -> None:
    readings = [_reading(20.0, 0), _reading(21.0, 60), _reading(22.0, 120)]
    svg = render_sparkline(readings)
    assert "<polyline" in svg
    assert len(_points(svg)) == 3


def test_flat_series_does_not_divide_by_zero() -> None:
    readings = [_reading(20.0, 0), _reading(20.0, 60), _reading(20.0, 120)]
    svg = render_sparkline(readings)
    assert "<polyline" in svg
    assert len(_points(svg)) == 3


def test_single_point_series_renders_without_crashing() -> None:
    svg = render_sparkline([_reading(20.0, 0)])
    assert "<polyline" in svg
    assert len(_points(svg)) == 1


def test_non_numeric_readings_are_ignored_gracefully() -> None:
    reading = ReadingRecord(location_id="pod-1", field="mode", timestamp=0, value="cool", unit=None)
    svg = render_sparkline([reading])
    assert "no data" in svg
    assert "<polyline" not in svg


def test_mixed_numeric_and_non_numeric_only_charts_the_numeric_ones() -> None:
    readings = [
        _reading(20.0, 0),
        ReadingRecord(
            location_id="pod-1", field="temperature", timestamp=60, value="n/a", unit=None
        ),
        _reading(22.0, 120),
    ]
    svg = render_sparkline(readings)
    assert len(_points(svg)) == 2


def test_svg_has_no_script_tag_or_cdn_reference() -> None:
    # The `xmlns="http://www.w3.org/2000/svg"` namespace URI is a standard,
    # never-fetched identifier (not a network reference) — deliberately not
    # asserted against here.
    svg = render_sparkline([_reading(20.0, 0), _reading(21.0, 60)])
    assert "<script" not in svg
    assert "cdn." not in svg.lower()
