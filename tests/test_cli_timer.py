"""Tests for ``sensibo timer`` — task t8.

Every test mocks ``sensibo.cli._commands.timer.SensiboClient`` — never a
real network call, never a real ``~/.sensibo`` key. Each write proves ZERO
calls to the mutating client method without ``--apply``, and exactly the
expected endpoint call with it.
"""

from __future__ import annotations

import json

import pytest

import sensibo.cli._commands.timer as timer_module
from sensibo.api import HttpError
from sensibo.cli import main
from sensibo.cli._errors import EXIT_ENV_ERROR


class _FakeClient:
    def __init__(self, timer: object | None = None) -> None:
        self.calls: list[tuple] = []
        self._timer = timer if timer is not None else {"id": "t1", "minutesFromNow": 30}

    def get_timer(self, pod_id: str) -> object:
        self.calls.append(("get_timer", pod_id))
        return self._timer

    def put_timer(self, pod_id: str, body: dict) -> object:
        self.calls.append(("put_timer", pod_id, body))
        return {"id": "t2", **body}

    def delete_timer(self, pod_id: str) -> object:
        self.calls.append(("delete_timer", pod_id))
        return None


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(timer_module, "SensiboClient", lambda *a, **kw: fake)


def _write_calls(fake: _FakeClient, name: str) -> list[tuple]:
    return [c for c in fake.calls if c[0] == name]


# --- show -----------------------------------------------------------------


def test_show_reads_through_the_mocked_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(["timer", "show", "pod1"])

    assert rc == 0
    assert fake.calls == [("get_timer", "pod1")]
    out = capsys.readouterr().out
    assert "pod1" in out
    assert "cloud (survives local daemon sleeping)" in out


def test_show_json_carries_cloud_execution_marker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(["timer", "show", "pod1", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution"] == "cloud (survives local daemon sleeping)"


# --- set: dry-run vs --apply ------------------------------------------------


def test_set_dry_run_makes_zero_write_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(["timer", "set", "pod1", "--minutes", "15", "--state", "off"])

    assert rc == 0
    assert _write_calls(fake, "put_timer") == []
    out = capsys.readouterr().out
    assert "applied: no" in out
    assert "cloud (survives local daemon sleeping)" in out


def test_set_apply_calls_put_timer_with_built_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(["timer", "set", "pod1", "--minutes", "15", "--state", "off", "--apply"])

    assert rc == 0
    calls = _write_calls(fake, "put_timer")
    assert calls == [("put_timer", "pod1", {"minutesFromNow": 15, "acState": {"on": False}})]


def test_set_state_on_carries_mode_temperature_and_fan_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(
        [
            "timer",
            "set",
            "pod1",
            "--minutes",
            "20",
            "--state",
            "on",
            "--mode",
            "cool",
            "--target-temperature",
            "21",
            "--fan-level",
            "high",
            "--apply",
        ]
    )

    assert rc == 0
    calls = _write_calls(fake, "put_timer")
    assert calls[0][2]["acState"] == {
        "on": True,
        "mode": "cool",
        "targetTemperature": 21,
        "fanLevel": "high",
    }


def test_set_raw_body_overrides_friendly_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    raw = json.dumps({"minutesFromNow": 5, "acState": {"on": True, "mode": "cool"}})
    rc = main(["timer", "set", "pod1", "--raw-body", raw, "--apply"])

    assert rc == 0
    calls = _write_calls(fake, "put_timer")
    assert calls == [("put_timer", "pod1", json.loads(raw))]


def test_set_missing_flags_is_a_user_error(capsys: pytest.CaptureFixture[str]) -> None:
    # A validation CliError raised inside cmd_set is caught by _dispatch and
    # returned as an int — not a SystemExit (that's argparse's own error path).
    rc = main(["timer", "set", "pod1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


def test_set_non_positive_minutes_is_a_user_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["timer", "set", "pod1", "--minutes", "0", "--state", "on"])
    assert rc == 1
    assert "hint:" in capsys.readouterr().err


# --- clear: dry-run vs --apply ----------------------------------------------


def test_clear_dry_run_makes_zero_write_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(["timer", "clear", "pod1"])

    assert rc == 0
    assert _write_calls(fake, "delete_timer") == []
    out = capsys.readouterr().out
    assert "applied: no" in out
    assert "cloud (survives local daemon sleeping)" in out


def test_clear_apply_calls_delete_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(["timer", "clear", "pod1", "--apply"])

    assert rc == 0
    assert _write_calls(fake, "delete_timer") == [("delete_timer", "pod1")]


def test_clear_json_carries_cloud_execution_marker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(["timer", "clear", "pod1", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution"] == "cloud (survives local daemon sleeping)"


# --- ApiError bubbling -------------------------------------------------------


class _RaisingClient:
    def get_timer(self, pod_id: str) -> object:
        raise HttpError(message="server exploded", status=500, remediation="retry later")


def test_show_api_error_maps_to_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timer_module, "SensiboClient", lambda *a, **kw: _RaisingClient())
    rc = main(["timer", "show", "pod1"])
    assert rc == EXIT_ENV_ERROR


def test_set_api_error_maps_to_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timer_module, "SensiboClient", lambda *a, **kw: _RaisingClient())
    rc = main(["timer", "set", "pod1", "--minutes", "10", "--state", "on"])
    assert rc == EXIT_ENV_ERROR


def test_clear_api_error_maps_to_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timer_module, "SensiboClient", lambda *a, **kw: _RaisingClient())
    rc = main(["timer", "clear", "pod1"])
    assert rc == EXIT_ENV_ERROR


# --- overview ---------------------------------------------------------------


def test_timer_overview_exists(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["timer", "overview"])
    assert rc == 0
    assert "sensibo timer" in capsys.readouterr().out


# --- explain -----------------------------------------------------------------


def test_timer_explain_entries_resolve(capsys: pytest.CaptureFixture[str]) -> None:
    for path in (["timer"], ["timer", "show"], ["timer", "set"], ["timer", "clear"]):
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        capsys.readouterr()
