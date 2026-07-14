"""Guard against `sensibo learn`'s command map drifting from the real CLI.

Qodo review 3581287831 (Maintainability): `learn` used to embed a manually
maintained command list — separate copies for its text output and its --json
payload — that would silently drift as verbs landed. `sensibo/explain/catalog.py`
now carries a single source of truth (`COMMAND_ORDER` + `SUMMARIES`) that both
`learn` outputs derive from; this test makes a future gap between the two an
actual test failure instead of something that only shows up as a stale doc.
"""

from __future__ import annotations

import argparse
import json

import pytest

from sensibo.cli import _build_parser, main
from sensibo.explain.catalog import COMMAND_ORDER, ENTRIES, SUMMARIES


def _registered_top_level_commands() -> set[str]:
    """Every verb name argparse actually accepts as `sensibo <verb> ...`."""
    parser = _build_parser()
    # argparse exposes no public accessor for "the subparsers action"; walking
    # the private group is the standard trick (mirrors what argparse itself
    # does internally when formatting --help).
    for action in parser._subparsers._group_actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("no top-level subparsers action found on the CLI parser")


def test_learn_json_commands_match_registered_top_level_verbs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every registered top-level verb appears in learn's JSON commands, and vice versa."""
    registered = _registered_top_level_commands()

    rc = main(["learn", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    learn_top_level = {cmd["path"][0] for cmd in payload["commands"]}

    missing_from_learn = registered - learn_top_level
    stale_in_learn = learn_top_level - registered
    assert not missing_from_learn, f"registered verb(s) missing from learn: {missing_from_learn}"
    assert not stale_in_learn, f"learn lists verb(s) no longer registered: {stale_in_learn}"


def test_learn_text_and_json_command_maps_agree(capsys: pytest.CaptureFixture[str]) -> None:
    """Text and --json are two views of one list; they must name the same verbs."""
    main(["learn"])
    text = capsys.readouterr().out
    main(["learn", "--json"])
    payload = json.loads(capsys.readouterr().out)

    for entry in payload["commands"]:
        invocation = "sensibo " + " ".join(entry["path"])
        assert invocation in text, f"{invocation!r} missing from learn's text Commands section"


def test_command_order_entries_have_summaries_and_resolve() -> None:
    """Every catalog COMMAND_ORDER entry has a summary and a real explain doc.

    Belt-and-suspenders alongside the module-level guard in
    sensibo/explain/catalog.py: this fails as an ordinary test (rather than an
    import-time RuntimeError) if the two ever fall out of sync again.
    """
    for path in COMMAND_ORDER:
        assert path in SUMMARIES, f"{path} has no SUMMARIES entry"
        assert path in ENTRIES, f"{path} has no ENTRIES markdown doc"
