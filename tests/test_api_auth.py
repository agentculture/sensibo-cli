"""Tests for sensibo.api._auth: API key resolution order.

Hard rule: never read the real ~/.sensibo/.env. Every test passes an explicit
``home`` (a pytest tmp_path) or monkeypatches the ``env`` mapping instead of
touching process-wide state, except where an env-var test needs monkeypatch to
prove the real os.environ seam works - and even then HOME is redirected to a
tmp_path first so a real dotenv file is never consulted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sensibo.api._auth import ENV_VAR, resolve_api_key
from sensibo.api._errors import MissingApiKeyError


def test_env_var_wins_when_present(tmp_path: Path) -> None:
    # A dotenv file is present too, with a *different* key - env must win.
    sensibo_dir = tmp_path / ".sensibo"
    sensibo_dir.mkdir()
    (sensibo_dir / ".env").write_text(f"{ENV_VAR}=from-dotenv\n", encoding="utf-8")

    key = resolve_api_key(env={ENV_VAR: "from-env"}, home=tmp_path)
    assert key == "from-env"


def test_falls_back_to_dotenv_when_env_var_absent(tmp_path: Path) -> None:
    sensibo_dir = tmp_path / ".sensibo"
    sensibo_dir.mkdir()
    (sensibo_dir / ".env").write_text(f"{ENV_VAR}=from-dotenv\n", encoding="utf-8")

    key = resolve_api_key(env={}, home=tmp_path)
    assert key == "from-dotenv"


def test_dotenv_parses_simple_key_value_lines_ignoring_noise(tmp_path: Path) -> None:
    sensibo_dir = tmp_path / ".sensibo"
    sensibo_dir.mkdir()
    (sensibo_dir / ".env").write_text(
        "\n".join(
            [
                "# a comment line",
                "",
                "SOME_OTHER_VAR=irrelevant",
                f'{ENV_VAR}="quoted-value"',
                "TRAILING=noise",
            ]
        ),
        encoding="utf-8",
    )

    key = resolve_api_key(env={}, home=tmp_path)
    assert key == "quoted-value"


def test_missing_key_raises_api_error_with_remediation(tmp_path: Path) -> None:
    with pytest.raises(MissingApiKeyError) as exc_info:
        resolve_api_key(env={}, home=tmp_path)

    err = exc_info.value
    assert err.code is not None
    assert err.message
    assert "home.sensibo.com/me/api" in err.remediation
    assert ENV_VAR in err.remediation


def test_empty_env_var_value_is_treated_as_absent(tmp_path: Path) -> None:
    sensibo_dir = tmp_path / ".sensibo"
    sensibo_dir.mkdir()
    (sensibo_dir / ".env").write_text(f"{ENV_VAR}=from-dotenv\n", encoding="utf-8")

    key = resolve_api_key(env={ENV_VAR: ""}, home=tmp_path)
    assert key == "from-dotenv"


def test_missing_dotenv_file_does_not_raise_unexpected_error(tmp_path: Path) -> None:
    # No .sensibo/.env at all under this tmp_path.
    with pytest.raises(MissingApiKeyError):
        resolve_api_key(env={}, home=tmp_path)


def test_resolve_uses_home_env_var_when_home_arg_omitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Production seam: HOME env var determines the dotenv path when ``home`` isn't passed."""
    sensibo_dir = tmp_path / ".sensibo"
    sensibo_dir.mkdir()
    (sensibo_dir / ".env").write_text(f"{ENV_VAR}=from-real-home-seam\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    key = resolve_api_key(env={})
    assert key == "from-real-home-seam"
