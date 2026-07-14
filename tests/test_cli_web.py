"""CLI tests for ``sensibo web`` (task t12).

The server itself is exercised end-to-end in ``tests/test_web_server.py``;
here we only check the CLI plumbing: argument parsing (``--bind``, ``--db``,
``--token-file``), that the token file is created with the right permissions
and its VALUE never printed, and that a monkeypatched ``_serve`` seam keeps
the test from actually blocking in ``serve_forever()``.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

import sensibo.cli._commands.web as web_module
from sensibo.cli import main
from sensibo.explain import known_paths
from sensibo.web import DEFAULT_BIND_HOST, DEFAULT_BIND_PORT, WebServer


@pytest.fixture()
def no_serve(monkeypatch: pytest.MonkeyPatch) -> dict[str, WebServer]:
    """Replace the blocking ``serve_forever()`` call with a no-op that just
    records the constructed server and closes it immediately."""
    captured: dict[str, WebServer] = {}

    def _fake_serve(server: WebServer) -> None:
        captured["server"] = server

    monkeypatch.setattr(web_module, "_serve", _fake_serve)
    return captured


# --- argparse wiring --------------------------------------------------------


def test_web_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["web", "--help"])
    assert exc.value.code == 0
    assert "web" in capsys.readouterr().out.lower()


def test_default_bind_constants_match_the_documented_lan_reachable_default() -> None:
    assert DEFAULT_BIND_HOST == "0.0.0.0"
    assert DEFAULT_BIND_PORT == 8323


def test_bind_flag_is_honored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_serve: dict[str, WebServer]
) -> None:
    db = tmp_path / "sensibo.db"
    token_file = tmp_path / "web-token"
    rc = main(["web", "--bind", "127.0.0.1:0", "--db", str(db), "--token-file", str(token_file)])
    assert rc == 0
    server = no_serve["server"]
    assert server.server_address[0] == "127.0.0.1"
    server.server_close()


def test_db_flag_is_passed_through_to_the_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_serve: dict[str, WebServer]
) -> None:
    db = tmp_path / "custom.db"
    token_file = tmp_path / "web-token"
    rc = main(["web", "--bind", "127.0.0.1:0", "--db", str(db), "--token-file", str(token_file)])
    assert rc == 0
    assert no_serve["server"].db_path == str(db)
    no_serve["server"].server_close()


def test_invalid_bind_value_is_a_structured_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["web", "--bind", "not-a-valid-bind", "--db", str(tmp_path / "x.db")])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


def test_invalid_port_in_bind_is_a_structured_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["web", "--bind", "127.0.0.1:not-a-port", "--db", str(tmp_path / "x.db")])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- the token: created 0600, path logged, value never logged ---------------


def test_token_file_is_created_mode_0600(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_serve: dict[str, WebServer]
) -> None:
    db = tmp_path / "sensibo.db"
    token_file = tmp_path / "nested" / "web-token"
    rc = main(["web", "--bind", "127.0.0.1:0", "--db", str(db), "--token-file", str(token_file)])
    assert rc == 0
    no_serve["server"].server_close()

    assert token_file.is_file()
    mode = stat.S_IMODE(token_file.stat().st_mode)
    assert mode == 0o600


def test_token_value_is_never_printed_only_its_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_serve: dict[str, WebServer]
) -> None:
    db = tmp_path / "sensibo.db"
    token_file = tmp_path / "web-token"
    rc = main(["web", "--bind", "127.0.0.1:0", "--db", str(db), "--token-file", str(token_file)])
    assert rc == 0
    no_serve["server"].server_close()
    captured = capsys.readouterr()

    token_value = token_file.read_text(encoding="utf-8").strip()
    assert token_value  # sanity: a token was actually generated
    assert token_value not in captured.out
    assert token_value not in captured.err
    assert str(token_file) in captured.err  # the path IS logged


def test_json_summary_never_leaks_the_token_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_serve: dict[str, WebServer]
) -> None:
    db = tmp_path / "sensibo.db"
    token_file = tmp_path / "web-token"
    rc = main(
        [
            "web",
            "--bind",
            "127.0.0.1:0",
            "--db",
            str(db),
            "--token-file",
            str(token_file),
            "--json",
        ]
    )
    assert rc == 0
    no_serve["server"].server_close()
    out = capsys.readouterr().out
    token_value = token_file.read_text(encoding="utf-8").strip()
    assert token_value not in out


def test_second_invocation_reuses_the_same_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_serve: dict[str, WebServer]
) -> None:
    db = tmp_path / "sensibo.db"
    token_file = tmp_path / "web-token"
    args = ["web", "--bind", "127.0.0.1:0", "--db", str(db), "--token-file", str(token_file)]

    rc = main(args)
    assert rc == 0
    no_serve["server"].server_close()
    first_token = token_file.read_text(encoding="utf-8").strip()

    rc = main(args)
    assert rc == 0
    no_serve["server"].server_close()
    second_token = token_file.read_text(encoding="utf-8").strip()

    assert first_token == second_token


# --- explain / doctor integration -------------------------------------------


def test_explain_web_entry_exists(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "web"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sensibo web" in out
    assert "token" in out.lower()


def test_web_is_a_known_explain_path() -> None:
    assert ("web",) in known_paths()


def test_doctor_still_healthy_after_adding_web(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["doctor", "--json"])
    assert rc == 0
