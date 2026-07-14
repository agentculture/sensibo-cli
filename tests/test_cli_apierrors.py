"""Tests for ``sensibo.cli._apierrors`` — the ApiError -> CliError bridge (task t8).

Every cloud-automation CLI verb funnels ``sensibo.api.ApiError`` through this
bridge so the top-level error contract holds even when the (mocked) client
raises. See ``sensibo/cli/_errors.py``'s exit-code policy.
"""

from __future__ import annotations

import json

import pytest

import sensibo.cli._commands.smartmode as smartmode_module
from sensibo.api import HttpError, MissingApiKeyError
from sensibo.cli import main
from sensibo.cli._commands._client import from_api_error
from sensibo.cli._errors import EXIT_ENV_ERROR, CliError


def test_from_api_error_maps_to_env_error_code() -> None:
    err = HttpError(message="boom", status=500, remediation="try again")
    cli_err = from_api_error(err)
    assert isinstance(cli_err, CliError)
    assert cli_err.code == EXIT_ENV_ERROR
    assert cli_err.message == "boom"
    assert cli_err.remediation == "try again"


class _RaisingClient:
    def get_smartmode(self, pod_id: str) -> object:
        raise MissingApiKeyError(
            message="no Sensibo API key found",
            remediation="set SENSIBO_API_KEY",
        )


def test_api_error_from_the_client_never_leaks_as_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A CliError raised from inside a handler body (as opposed to an argparse
    # parse-time error) is caught by `_dispatch` and returned as an int exit
    # code from `main()` — it does not raise SystemExit.
    monkeypatch.setattr(smartmode_module, "SensiboClient", lambda *a, **kw: _RaisingClient())

    rc = main(["smartmode", "show", "pod1"])

    assert rc == EXIT_ENV_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_api_error_json_mode_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(smartmode_module, "SensiboClient", lambda *a, **kw: _RaisingClient())

    rc = main(["smartmode", "show", "pod1", "--json"])

    assert rc == EXIT_ENV_ERROR
    payload = json.loads(capsys.readouterr().err)
    assert set(payload) == {"code", "message", "remediation"}
    assert payload["code"] == EXIT_ENV_ERROR
