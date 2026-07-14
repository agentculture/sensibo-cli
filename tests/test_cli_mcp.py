"""CLI tests for ``sensibo mcp`` — the MCP server verb (task t11).

Written first (TDD): these fail until ``sensibo/cli/_commands/mcp.py`` is
registered. Two contracts under test:

1. **Missing SDK -> CliError, not a traceback.** ``mcp`` is a dev-group
   dependency here (needed to test the "installed" path at all), so the
   "not installed" path is simulated via
   ``monkeypatch.setitem(sys.modules, "mcp", None)`` — Python's import
   machinery raises ``ImportError`` for any name mapped to ``None`` in
   ``sys.modules``, which is exactly what an actually-missing package would
   do, without needing to uninstall anything.
2. **The core CLI never imports ``mcp`` eagerly.** Building the parser
   happens on every invocation, whatever verb — ``sensibo mcp --help`` (which
   never reaches ``cmd_mcp_serve``) must not require the SDK to be present at
   all, proven the same way (SDK "absent" via the same monkeypatch, verb
   still resolves and prints help).
"""

from __future__ import annotations

import json
import sys

import pytest

from sensibo.cli import main
from sensibo.explain import known_paths

# --- missing-SDK path: CliError with the remediation, not a traceback ------


def test_serve_without_sdk_raises_remediation_cli_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "mcp", None)

    rc = main(["mcp", "serve"])

    assert rc == 2  # EXIT_ENV_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert 'pip install "sensibo-cli[mcp]"' in err
    assert "Traceback" not in err


def test_serve_without_sdk_json_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "mcp", None)

    rc = main(["mcp", "serve", "--json"])

    assert rc == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == 2
    assert 'pip install "sensibo-cli[mcp]"' in payload["remediation"]


def test_mcp_help_does_not_require_the_sdk(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``sensibo mcp --help`` never reaches ``cmd_mcp_serve`` — must not need ``mcp``."""
    monkeypatch.setitem(sys.modules, "mcp", None)

    with pytest.raises(SystemExit) as exc:
        main(["mcp", "--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "serve" in out


# --- SDK present: cmd_mcp_serve actually starts the server -----------------


def test_serve_with_sdk_present_invokes_run_stdio(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sensibo.mcp_server as mcp_server_module

    calls: list[bool] = []
    monkeypatch.setattr(mcp_server_module, "run_stdio", lambda: calls.append(True))

    rc = main(["mcp", "serve"])

    assert rc == 0
    assert calls == [True]
    err = capsys.readouterr().err
    assert "stdio" in err


# --- bare noun / overview ---------------------------------------------------


def test_mcp_bare_noun_prints_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["mcp"])
    assert rc == 0
    assert capsys.readouterr().out.strip()


def test_mcp_overview_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["mcp", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "sensibo mcp"
    assert isinstance(payload["sections"], list)
    assert payload["sections"]


def test_mcp_overview_text_lists_the_five_tools(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["mcp", "overview"])
    assert rc == 0
    out = capsys.readouterr().out
    for tool in (
        "list_devices",
        "read_location",
        "query_history",
        "set_ac_state",
        "room_list",
    ):
        assert tool in out


# --- naming: usage/hints say `sensibo`, never `sensibo-cli` ------------------


def test_mcp_usage_names_the_installed_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["mcp", "--help"])
    out = capsys.readouterr().out
    assert "usage: sensibo mcp" in out
    assert "usage: sensibo-cli" not in out


def test_mcp_bad_subverb_hint_names_the_installed_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["mcp", "bogus-subverb"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "hint:" in err
    assert "sensibo-cli --help" not in err
    assert "sensibo mcp --help" in err or "sensibo --help" in err


# --- explain catalog entries --------------------------------------------------


def test_mcp_paths_are_in_the_explain_catalog() -> None:
    paths = known_paths()
    assert ("mcp",) in paths
    assert ("mcp", "serve") in paths
    assert ("mcp", "overview") in paths


def test_explain_mcp_paths_resolve(capsys: pytest.CaptureFixture[str]) -> None:
    for path in (("mcp",), ("mcp", "serve"), ("mcp", "overview")):
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        capsys.readouterr()


def test_explain_mcp_mentions_the_extra_and_apply_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["explain", "mcp"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sensibo-cli[mcp]" in out
    assert "apply" in out.lower()
