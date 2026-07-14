"""Shared cloud-execution marker for Sensibo's SERVER-SIDE automation verbs.

Climate React (``smartmode``), ``schedule``, and ``timer`` all run **inside
Sensibo's cloud**, not on this operator's machine. That is a meaningful
difference from a local rules engine (a later task, ``sensibo/rules/``):
these keep enforcing themselves even when the local daemon/collector is
asleep, offline, or was never started. Every verb built on top of these
endpoints must say so, in both text and ``--json`` output — this module is
the one place that wording lives, so it can't drift between verbs.
"""

from __future__ import annotations

EXECUTION_FIELD = "execution"
EXECUTION_NOTE = "cloud (survives local daemon sleeping)"


def execution_marker() -> dict[str, str]:
    """The ``{"execution": "..."}`` fragment every JSON payload here carries."""
    return {EXECUTION_FIELD: EXECUTION_NOTE}


def execution_text_line() -> str:
    """The matching single line for text-mode output."""
    return f"{EXECUTION_FIELD}: {EXECUTION_NOTE}"
