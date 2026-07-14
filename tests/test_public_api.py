"""Tests for task t10 — ``import sensibo`` as a documented public library surface.

The claim under test: a third-party script can ``import sensibo``, instantiate a
client, list pods, read current measurements, read history, and set AC state —
with **zero** CLI or argparse involvement. Two things must hold simultaneously:

1. ``import sensibo`` alone must never pull in :mod:`argparse` or
   :mod:`sensibo.cli` — verified via ``sys.modules`` inspection *in a fresh
   subprocess*, because within this same pytest process other test modules
   (``tests/test_cli.py`` and friends) already import ``sensibo.cli`` and would
   make an in-process check meaningless.
2. The re-exported surface is actually usable end-to-end against a
   mocked/injected transport — never a real network call, never a real key.

Written first (TDD): these fail against the scaffold ``sensibo/__init__.py``
(only ``__version__``) and pass once the re-exports described in the t10 task
land.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "api.md"

# --- (1) `import sensibo` alone must stay light -----------------------------


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=30,
    )


def test_fresh_import_of_sensibo_does_not_pull_in_argparse() -> None:
    """A bare ``import sensibo`` must never load argparse.

    Checked in a fresh subprocess: this test module's own process has already
    imported argparse via other test modules, so an in-process
    ``sys.modules`` check would prove nothing.
    """
    result = _run(
        "import sys\n"
        "import sensibo\n"
        "assert 'argparse' not in sys.modules, sorted(sys.modules)\n"
        "print('OK')\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def test_fresh_import_of_sensibo_does_not_import_the_cli_subpackage() -> None:
    """A bare ``import sensibo`` must never load :mod:`sensibo.cli`.

    The CLI subpackage is a separate, heavier layer (argparse, dispatch,
    error/stream contracts) that only loads on an explicit
    ``import sensibo.cli`` or the ``sensibo`` console script.
    """
    result = _run(
        "import sys\n"
        "import sensibo\n"
        "assert 'sensibo.cli' not in sys.modules, sorted(sys.modules)\n"
        "print('OK')\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def test_importing_sensibo_cli_still_works_afterwards() -> None:
    """Re-exporting from sensibo/__init__.py must not break `import sensibo.cli`.

    Backward compatibility: cli modules do `from sensibo import __version__`,
    and the console script imports `sensibo.cli`. Both must keep working.
    """
    result = _run(
        "import sensibo\n"
        "import sensibo.cli\n"
        "assert sensibo.cli.main is not None\n"
        "print('OK')\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


# --- (2) the re-exported surface --------------------------------------------


def test_public_reexports_are_present_on_the_sensibo_module() -> None:
    import sensibo

    # The minimum surface the t10 task names explicitly.
    assert sensibo.SensiboClient is not None
    assert sensibo.Client is sensibo.SensiboClient  # documented alias
    assert sensibo.resolve_api_key is not None
    assert sensibo.ApiError is not None
    assert sensibo.Store is not None
    assert "__all__" in vars(sensibo)
    for name in sensibo.__all__:
        assert hasattr(sensibo, name), f"__all__ names {name!r} but it isn't an attribute"


def test_error_family_is_reexported() -> None:
    import sensibo

    for name in (
        "ApiError",
        "MissingApiKeyError",
        "HttpError",
        "RateLimitExceededError",
        "GatedHistoryWindowError",
    ):
        assert hasattr(sensibo, name), f"missing {name}"
        assert issubclass(getattr(sensibo, name), sensibo.ApiError)


def test_reexports_are_the_same_objects_as_the_originating_submodules() -> None:
    """Re-exports must be aliases, not copies — isinstance/except must keep working."""
    import sensibo
    import sensibo.api
    import sensibo.store

    assert sensibo.SensiboClient is sensibo.api.SensiboClient
    assert sensibo.ApiError is sensibo.api.ApiError
    assert sensibo.resolve_api_key is sensibo.api.resolve_api_key
    assert sensibo.Store is sensibo.store.Store


# --- (3) end-to-end library usage, zero CLI, mocked transport --------------

_THIRD_PARTY_SCRIPT = """
import sys

# `import sensibo` must be checked before anything else of this script's own
# choosing (e.g. `unittest.mock`, used below purely for this test's transport
# injection) pulls in argparse itself — that would attribute a leak to
# `sensibo` that is actually this script's own tooling.
import sensibo

assert "argparse" not in sys.modules, "argparse leaked from `import sensibo`"
assert "sensibo.cli" not in sys.modules, "sensibo.cli leaked from `import sensibo`"

import json  # noqa: E402 - see ordering note above
from unittest.mock import patch  # noqa: E402 - see ordering note above

FAKE_PODS = {"result": [{"id": "pod1", "productModel": "airq"}]}
FAKE_MEASUREMENTS = {"temperature": 21.5, "humidity": 44}
FAKE_HISTORY = {"result": [{"temperature": 21.0, "time": {"time": "2026-07-14T00:00:00Z"}}]}
FAKE_SET_STATE = {"result": {"acState": {"on": True, "mode": "cool"}}}


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = {}

    def read(self):
        return self._body

    def close(self):
        pass


calls = []


def fake_urlopen(req, timeout=None):
    calls.append((req.get_method(), req.full_url))
    url = req.full_url
    if "historicalMeasurements" in url:
        return FakeResponse(FAKE_HISTORY)
    if url.split("?")[0].endswith("/measurements"):
        return FakeResponse(FAKE_MEASUREMENTS)
    if "/acStates" in url:
        return FakeResponse(FAKE_SET_STATE)
    return FakeResponse(FAKE_PODS)


with patch("sensibo.api.client.urlopen", fake_urlopen):
    key = sensibo.resolve_api_key(env={"SENSIBO_API_KEY": "THIRD-PARTY-TEST-KEY"})
    client = sensibo.Client(api_key=key, min_interval=0)

    pods = client.get_pods()
    assert pods == FAKE_PODS, pods

    measurements = client.get_measurements("pod1")
    assert measurements == FAKE_MEASUREMENTS, measurements

    history = client.get_historical_measurements("pod1", days=1)
    assert history == FAKE_HISTORY, history

    result = client.post_ac_states("pod1", {"on": True, "mode": "cool"})
    assert result == FAKE_SET_STATE, result

assert "THIRD-PARTY-TEST-KEY" not in repr(client)
methods = {method for method, _ in calls}
assert methods == {"GET", "POST"}, methods
print("OK")
"""


def test_third_party_script_lists_pods_reads_and_sets_state_with_mocked_transport(
    tmp_path: Path,
) -> None:
    """End-to-end: list pods, read measurements, read history, set state.

    Runs as a real subprocess script — not an in-process call — so it proves
    the surface works for a script that does nothing but ``import sensibo``,
    with the one HTTP seam (``urlopen``) mocked and no CliError/argparse in
    the picture at all.
    """
    script = tmp_path / "third_party_script.py"
    script.write_text(_THIRD_PARTY_SCRIPT, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


# --- doc/code alignment: every documented symbol must actually exist -------


def _documented_symbol_names() -> set[str]:
    """Extract every ``sensibo.<Name>`` and ``from sensibo import ...`` symbol.

    Two patterns cover how docs/api.md references the public surface:
    ``sensibo.PascalCase`` attribute access (deliberately capital-first only,
    so prose mentions of the ``home.sensibo.com`` domain never false-positive
    into a bogus symbol name), and
    ``from sensibo import name_a, name_b`` import lists (which is how the doc
    references lowercase functions/constants).
    """
    text = DOC_PATH.read_text(encoding="utf-8")
    names: set[str] = set()

    for match in re.finditer(r"\bsensibo\.([A-Z][A-Za-z0-9_]*)\b", text):
        names.add(match.group(1))

    for match in re.finditer(r"from sensibo import \(?([^)\n]+)\)?", text):
        for piece in match.group(1).split(","):
            piece = piece.strip().strip("\\").strip()
            if piece:
                names.add(piece)

    # Not real attributes of the `sensibo` module: submodule names sometimes
    # appear in prose/paths (e.g. `sensibo.cli`) and are irrelevant here since
    # they are lowercase package names, never symbols this task re-exports.
    names -= {"cli", "api", "store", "explain"}
    return names


def test_docs_api_md_exists_and_is_non_trivial() -> None:
    assert DOC_PATH.is_file(), "docs/api.md must exist (task t10 deliverable 2)"
    assert len(DOC_PATH.read_text(encoding="utf-8")) > 500


def test_every_symbol_documented_in_docs_api_md_actually_exists() -> None:
    """Doc examples' names verified against the actual exports."""
    import sensibo

    documented = _documented_symbol_names()
    assert documented, "expected docs/api.md to reference at least one sensibo symbol"
    missing = sorted(name for name in documented if not hasattr(sensibo, name))
    assert not missing, f"docs/api.md references undefined sensibo symbols: {missing}"


def test_docs_api_md_covers_the_minimum_required_surface() -> None:
    """The task's named minimum surface must all appear somewhere in the doc."""
    text = DOC_PATH.read_text(encoding="utf-8")
    for required in (
        "SensiboClient",
        "resolve_api_key",
        "ApiError",
        "Store",
        "SENSIBO_API_KEY",
        ".sensibo/.env",
    ):
        assert required in text, f"docs/api.md must mention {required!r}"


def test_docs_api_md_notes_the_days_one_gated_history_window() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    assert "days=1" in text or "days: int = 1" in text or "`days`" in text
    assert "403" in text
    assert "GatedHistoryWindowError" in text


def test_docs_api_md_notes_write_is_immediate_at_this_layer() -> None:
    """The library layer has no dry-run contract — that must be stated, not assumed."""
    text = DOC_PATH.read_text(encoding="utf-8").lower()
    assert "immediate" in text
    assert "dry-run" in text


def test_docs_api_md_notes_key_never_logged_unscrubbed() -> None:
    text = DOC_PATH.read_text(encoding="utf-8").lower()
    assert "never log" in text or "never logged" in text or "scrub" in text
