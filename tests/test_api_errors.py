"""Tests for sensibo.api's error shape (mirrors CliError without importing it)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sensibo.api._errors import (
    ERROR_AUTH,
    ERROR_GATED,
    ERROR_NETWORK,
    ERROR_RATE_LIMIT,
    ApiError,
    GatedHistoryWindowError,
    HttpError,
    MissingApiKeyError,
    RateLimitExceededError,
)


def test_api_error_carries_code_message_remediation() -> None:
    err = ApiError(code=1, message="boom", remediation="try again")
    assert err.code == 1
    assert err.message == "boom"
    assert err.remediation == "try again"
    # str(Exception) reflects the message, matching CliError's shape.
    assert str(err) == "boom"


def test_api_error_default_remediation_is_empty_string() -> None:
    err = ApiError(code=1, message="boom")
    assert err.remediation == ""


def test_api_error_to_dict() -> None:
    err = ApiError(code=2, message="msg", remediation="fix it")
    assert err.to_dict() == {"code": 2, "message": "msg", "remediation": "fix it"}


def test_api_error_is_an_exception() -> None:
    with pytest.raises(ApiError):
        raise ApiError(code=1, message="boom")


def test_missing_api_key_error_uses_auth_code() -> None:
    err = MissingApiKeyError(message="no key", remediation="set one")
    assert isinstance(err, ApiError)
    assert err.code == ERROR_AUTH
    assert err.message == "no key"
    assert err.remediation == "set one"


def test_http_error_carries_status_and_network_code() -> None:
    err = HttpError(message="HTTP 500", status=500, remediation="retry later")
    assert isinstance(err, ApiError)
    assert err.code == ERROR_NETWORK
    assert err.status == 500


def test_rate_limit_exceeded_error_carries_429() -> None:
    err = RateLimitExceededError(message="rate limited", remediation="slow down")
    assert isinstance(err, ApiError)
    assert err.code == ERROR_RATE_LIMIT
    assert err.status == 429


def test_gated_history_window_error_carries_pod_and_days() -> None:
    err = GatedHistoryWindowError(
        pod_id="abc123", days=30, message="gated", remediation="use days=1"
    )
    assert isinstance(err, ApiError)
    assert err.code == ERROR_GATED
    assert err.status == 403
    assert err.pod_id == "abc123"
    assert err.days == 30


# --- standalone-library guard: sensibo.api must not import sensibo.cli ----


def _imported_module_names(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_api_package_does_not_import_cli_or_argparse() -> None:
    """sensibo/api/ must be usable as a standalone library (no CLI, no argparse).

    A runtime ``sys.modules`` check would be order-dependent (other test
    modules import sensibo.cli first), so this statically scans the source.
    """
    api_dir = Path(__file__).resolve().parent.parent / "sensibo" / "api"
    py_files = list(api_dir.glob("*.py"))
    assert py_files, "expected sensibo/api/*.py to exist"

    offenders: list[str] = []
    for py_file in py_files:
        for name in _imported_module_names(py_file):
            if name == "argparse" or name == "sensibo.cli" or name.startswith("sensibo.cli."):
                offenders.append(f"{py_file.name} imports {name}")
    assert not offenders, f"sensibo/api/ must not depend on the CLI layer: {offenders}"
