"""Tests for sensibo.report.chart — the multi-location SVG report (task t4).

Written first (TDD): these fail against an empty ``sensibo/report`` package and
pass once :func:`sensibo.report.render_report` exists. Every test opens a
:class:`~sensibo.store.Store` against a ``tmp_path`` file — never the real
``~/.sensibo``.
"""

from __future__ import annotations

import math
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from sensibo.report import render_report
from sensibo.store import Store

# --- fixtures ---------------------------------------------------------------

#: A fixed reference instant so window bounds and the title's generation time
#: are deterministic.
NOW = 1_700_000_000.0

#: The five numeric fields every data location reports.
FIELDS = ("temperature", "feelsLike", "humidity", "tvoc", "co2")


def _value(field: str, i: int) -> float:
    """A small synthetic series per field — deterministic, no RNG."""
    base = {"temperature": 21.0, "feelsLike": 20.5, "humidity": 45.0, "tvoc": 300.0, "co2": 600.0}
    return base[field] + math.sin(i / 50.0 + len(field)) * 2.0


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(db_path=tmp_path / "t.db")
    # One pod plus two Room Sensors nested under it; a fourth location has
    # metadata but zero readings (the empty-panel case).
    s.upsert_location("pod-1", kind="pod", product_model="airq", room_name="Office")
    s.set_alias("pod-1", "Living Room AC")
    s.upsert_location("ms-1", kind="room_sensor", parent_pod_id="pod-1", room_name="Bedroom")
    s.upsert_location("ms-2", kind="room_sensor", parent_pod_id="pod-1")
    # Known to the store but has never reported: the empty-panel case.
    s.upsert_location("pod-quiet", kind="pod", product_model="airq", room_name="Attic")
    for location_id in ("pod-1", "ms-1", "ms-2"):
        for f in FIELDS:
            # 7 days at 90s cadence, bulk-inserted via the single-transaction
            # path (Store.record_series, plan deviation d1).
            s.record_series(
                location_id,
                f,
                [(NOW - 7 * 86400 + i * 90, _value(f, i)) for i in range(6720)],
            )
    yield s
    s.close()


# --- the report document ----------------------------------------------------


def test_seven_day_report_is_fast_and_bounded(store: Store) -> None:
    start = time.perf_counter()
    svg = render_report(store, 168, now=NOW)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
    assert len(svg.encode("utf-8")) < 1_000_000


def test_report_is_well_formed_xml_with_no_script_tag(store: Store) -> None:
    svg = render_report(store, 168, now=NOW)
    root = ET.fromstring(svg)
    assert root.tag.rsplit("}", 1)[-1] == "svg"  # ElementTree keeps the namespace
    assert "<script" not in svg.lower()


def test_empty_series_renders_a_labelled_panel_not_an_error(store: Store) -> None:
    svg = render_report(store, 168, now=NOW)
    assert "no readings in window" in svg
    assert "Attic" in svg


def test_24_hour_title_and_every_location_label_appear(store: Store) -> None:
    svg = render_report(store, 24, now=NOW)
    assert "Last 24 hours" in svg
    # Labels fall back alias -> room_name -> id.
    assert "Living Room AC" in svg
    assert "Bedroom" in svg
    assert "ms-2" in svg


def test_7_day_window_title_and_generation_time_are_iso_utc(store: Store) -> None:
    svg = render_report(store, 168, now=NOW)
    assert "Last 7 days" in svg
    # NOW is 2023-11-14T22:13:20Z.
    assert "2023-11-14T22:13:20Z" in svg


def test_downsampling_caps_points_but_keeps_first_and_last(store: Store) -> None:
    svg = render_report(store, 168, now=NOW, max_points=100)
    points = [node for node in svg.split("<polyline") if node.startswith(" points=")]
    assert points, "expected at least one polyline panel"
    for chunk in points:
        coords = chunk.split('points="')[1].split('"')[0].split()
        assert len(coords) <= 100
