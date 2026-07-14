"""Boundary and positioning guards (task t3).

These tests enforce the product boundaries recorded in
``docs/specs/2026-07-14-sensibo-cli-ships-the-full-product-one-cli-and-age.md``
(Scope / boundaries) and
``docs/plans/2026-07-14-sensibo-cli-ships-the-full-product-one-cli-and-age.md``
(task t3):

(a) unofficial community tool — the trademark disclaimer,
(b) cloud transport only — "local" never means a LAN-local Sensibo protocol,
(c) not a general home-automation platform — Sensibo devices only.

They are doc/grep guards, not behavioural tests: they read committed files
directly so a future edit that quietly drops the disclaimer, the cloud-only
finding, the before-state record, or introduces a non-Sensibo vendor
reference fails CI immediately instead of drifting silently.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Vendor names that must never appear in shipped source as a *control target*.
# Matched as whole words (case-insensitive) so "nested" does not false-positive
# on "nest".
_NON_SENSIBO_VENDORS = ("tuya", "broadlink", "tado", "nest", "ecobee", "homekit")
_VENDOR_PATTERN = re.compile(r"\b(" + "|".join(_NON_SENSIBO_VENDORS) + r")\b", re.IGNORECASE)


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


# --- (a) trademark / unofficial-tool disclaimer ----------------------------


def test_readme_carries_the_trademark_disclaimer() -> None:
    """README keeps the unofficial-tool disclaimer, not just `learn` output."""
    readme = _read("README.md")
    assert "Unofficial community tool" in readme
    assert "trademark of Sensibo Ltd" in readme


# --- (b) + (c) boundary / positioning section in the README ----------------


def test_readme_has_boundary_positioning_section() -> None:
    """README states the cloud-only and not-a-platform boundaries plainly."""
    readme = _read("README.md")
    assert "## Scope and boundaries" in readme
    # (b) cloud transport only; "local" means where data lands, not the wire.
    assert "cloud-only" in readme.lower() or "cloud only" in readme.lower()
    # (c) not a general home-automation platform.
    assert "not a general home-automation platform" in readme.lower()
    assert "home assistant" in readme.lower()


def test_sensibo_api_doc_keeps_cloud_only_finding() -> None:
    """The evidence for the cloud-only finding must stay in sensibo-api.md.

    Guards against the finding migrating out of its authoritative home (or
    being softened) while other docs merely reference it.
    """
    doc = _read("docs/sensibo-api.md")
    assert "## The load-bearing question: is there a local API?" in doc
    assert "No — Sensibo is cloud-only (CONFIRMED)." in doc


# --- (before-state) project history --------------------------------------


def test_history_doc_records_the_before_state() -> None:
    """The frame-time before-state (scaffold only) is recorded with evidence.

    Per the spec's honesty condition: "at frame time the repo ships only
    introspection verbs, with no control, collect, or rules code" — anchored
    to the frame commit so the claim is checkable, not just asserted.
    """
    doc = _read("docs/history.md")
    assert "f373915" in doc, "before-state note must cite the frame commit hash"
    assert "2026-07-14" in doc
    for verb in ("whoami", "learn", "explain", "overview", "doctor"):
        assert verb in doc
    lowered = doc.lower()
    assert "scaffold" in lowered
    assert "no control" in lowered or "no ac control" in lowered


# --- (c) no non-Sensibo device vendors as control targets -------------------


def _iter_source_files() -> list[Path]:
    return sorted((REPO_ROOT / "sensibo").rglob("*.py"))


def test_no_source_file_references_non_sensibo_vendors() -> None:
    """CLI verbs, help text, and the explain catalog target Sensibo only.

    Guards boundary (c): this is not a general home-automation platform.
    Scans ``sensibo/`` source only — docs are allowed to *discuss* a vendor
    (e.g. sensibo-api.md's HomeKit-on-Air-Pro analysis) without shipping it as
    a control target.
    """
    offenders: list[str] = []
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8")
        for match in _VENDOR_PATTERN.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {match.group(0)}")
    assert not offenders, "non-Sensibo vendor reference(s) found: " + ", ".join(offenders)
