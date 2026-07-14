"""Tests for sensibo.web._token — the write-auth token: generate, persist, never log.

Written first (TDD): these fail against an empty ``sensibo/web`` package and
pass once ``sensibo/web/_token.py`` lands (task t12). Every test uses a
``tmp_path`` token file — never the real ``~/.sensibo``.
"""

from __future__ import annotations

import stat
from pathlib import Path

from sensibo.web._token import check_token, ensure_token


def test_first_run_generates_and_persists_a_token(tmp_path: Path) -> None:
    token_path = tmp_path / "nested" / "web-token"
    token, used_path = ensure_token(token_path)

    assert used_path == token_path
    assert token_path.is_file()
    assert token_path.read_text(encoding="utf-8").strip() == token
    assert len(token) >= 32


def test_token_file_is_mode_0600(tmp_path: Path) -> None:
    token_path = tmp_path / "web-token"
    ensure_token(token_path)
    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600


def test_parent_directory_is_mode_0700(tmp_path: Path) -> None:
    token_path = tmp_path / "nested" / "web-token"
    ensure_token(token_path)
    mode = stat.S_IMODE(token_path.parent.stat().st_mode)
    assert mode == 0o700


def test_second_call_reuses_the_persisted_token(tmp_path: Path) -> None:
    token_path = tmp_path / "web-token"
    first, _ = ensure_token(token_path)
    second, _ = ensure_token(token_path)
    assert first == second


def test_two_generated_tokens_differ(tmp_path: Path) -> None:
    first, _ = ensure_token(tmp_path / "a" / "web-token")
    second, _ = ensure_token(tmp_path / "b" / "web-token")
    assert first != second


def test_check_token_matches_and_rejects(tmp_path: Path) -> None:
    token, _ = ensure_token(tmp_path / "web-token")
    assert check_token(token, token) is True
    assert check_token("wrong-value", token) is False
    assert check_token(None, token) is False
    assert check_token("", token) is False
