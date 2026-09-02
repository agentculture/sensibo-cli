"""Smoke tests for the sensibo-cli CLI entry point and its verbs."""

from __future__ import annotations

import json

import pytest

from sensibo import __version__
from sensibo.cli import main
from sensibo.explain import known_paths


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 0
    assert "usage: sensibo" in capsys.readouterr().out


def test_unknown_command_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- whoami ---------------------------------------------------------------


def test_whoami_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["whoami"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nick: sensibo-cli" in out
    assert "backend: colleague" in out
    assert "model:" in out


def test_whoami_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["whoami", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["nick"] == "sensibo-cli"
    assert payload["version"] == __version__
    assert payload["backend"] == "colleague"


# --- learn ----------------------------------------------------------------


def test_learn_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn"])
    assert rc == 0
    out = capsys.readouterr().out
    assert len(out) >= 200
    assert "sensibo-cli" in out
    assert "Exit-code policy" in out
    assert "--json" in out
    assert "explain" in out


def test_learn_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "sensibo"
    assert payload["dist"] == "sensibo-cli"
    assert "trademark of Sensibo Ltd" in payload["disclaimer"]
    assert payload["version"] == __version__
    assert payload["json_support"] is True


# --- explain --------------------------------------------------------------


def test_explain_root(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain"])
    assert rc == 0
    assert "# sensibo-cli" in capsys.readouterr().out


def test_explain_self(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "sensibo-cli"])
    assert rc == 0
    assert capsys.readouterr().out.startswith("#")


def test_explain_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "whoami", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == ["whoami"]
    assert "sensibo whoami" in payload["markdown"]


def test_explain_unknown_path_errors(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "nonexistent"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "hint:" in captured.err


def test_every_catalog_path_resolves(capsys: pytest.CaptureFixture[str]) -> None:
    for path in known_paths():
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        capsys.readouterr()


# --- naming: the console command is `sensibo`, the dist is `sensibo-cli` ---


def test_usage_names_the_installed_command(capsys: pytest.CaptureFixture[str]) -> None:
    """argparse `prog` must be the console command, not the dist name.

    Regression guard: `prog="sensibo-cli"` made `--help` print
    `usage: sensibo-cli ...` and made parse-error remediations tell the user to
    run `sensibo-cli --help` — a command that pip never installs. The entry
    point in pyproject.toml is `sensibo`.
    """
    main([])
    out = capsys.readouterr().out
    assert "usage: sensibo " in out or out.startswith("usage: sensibo\n")
    assert "usage: sensibo-cli" not in out


def test_parse_error_hint_names_the_installed_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["bogus"])
    err = capsys.readouterr().err
    assert "hint:" in err
    assert "sensibo-cli --help" not in err


def test_learn_carries_the_trademark_disclaimer(capsys: pytest.CaptureFixture[str]) -> None:
    """The unofficial-tool disclaimer is required in `learn` output, not just the README."""
    main(["learn"])
    out = capsys.readouterr().out
    assert "Unofficial community tool" in out
    assert "trademark of Sensibo Ltd" in out


def test_learn_does_not_claim_to_be_a_template(capsys: pytest.CaptureFixture[str]) -> None:
    main(["learn"])
    assert "clonable template" not in capsys.readouterr().out


# --- t6: query health / notify explain + learn coverage ---------------------


def test_explain_query_health(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "query", "health"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sensibo query health" in out


def test_explain_notify(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "notify"])
    assert rc == 0
    assert "sensibo notify" in capsys.readouterr().out


def test_explain_notify_test(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "notify", "test"])
    assert rc == 0
    assert "notify test" in capsys.readouterr().out


def test_learn_mentions_health_and_notify_and_local_execution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["learn"])
    out = capsys.readouterr().out
    assert "health" in out.lower()
    assert "notify" in out.lower()
    assert "local (stops when this daemon stops)" in out


def test_learn_json_mentions_health_and_notify_and_local_execution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["learn", "--json"])
    payload = json.loads(capsys.readouterr().out)
    blob = json.dumps(payload).lower()
    assert "health" in blob
    assert "notify" in blob
    assert "local (stops when this daemon stops)" in blob
