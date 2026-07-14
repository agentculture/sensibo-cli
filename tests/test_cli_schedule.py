"""Tests for ``sensibo schedule`` — task t8.

Every test mocks ``sensibo.cli._commands.schedule.SensiboClient`` — never a
real network call, never a real ``~/.sensibo`` key. Each write proves ZERO
calls to the mutating client method without ``--apply``, and exactly the
expected endpoint call with it.
"""

from __future__ import annotations

import json

import pytest

import sensibo.cli._commands.schedule as schedule_module
from sensibo.api import HttpError
from sensibo.cli import main
from sensibo.cli._errors import EXIT_ENV_ERROR


class _FakeClient:
    def __init__(self, schedules: object | None = None) -> None:
        self.calls: list[tuple] = []
        self._schedules = schedules if schedules is not None else {"result": []}

    def get_schedules(self, pod_id: str) -> object:
        self.calls.append(("get_schedules", pod_id))
        return self._schedules

    def post_schedules(self, pod_id: str, body: dict) -> object:
        self.calls.append(("post_schedules", pod_id, body))
        return {"id": "new-sched", **body}

    def delete_schedule(self, pod_id: str, schedule_id: str) -> object:
        self.calls.append(("delete_schedule", pod_id, schedule_id))
        return None


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(schedule_module, "SensiboClient", lambda *a, **kw: fake)


def _write_calls(fake: _FakeClient, name: str) -> list[tuple]:
    return [c for c in fake.calls if c[0] == name]


# --- list ---------------------------------------------------------------


def test_list_reads_through_the_mocked_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient(schedules={"result": [{"id": "s1"}]})
    _install_fake(monkeypatch, fake)

    rc = main(["schedule", "list", "pod1"])

    assert rc == 0
    assert fake.calls == [("get_schedules", "pod1")]
    out = capsys.readouterr().out
    assert "pod1" in out
    assert "cloud (survives local daemon sleeping)" in out


def test_list_json_carries_cloud_execution_marker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(["schedule", "list", "pod1", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution"] == "cloud (survives local daemon sleeping)"


# --- create: dry-run vs --apply ------------------------------------------


def test_create_dry_run_makes_zero_write_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(["schedule", "create", "pod1", "--time", "22:30"])

    assert rc == 0
    assert _write_calls(fake, "post_schedules") == []
    out = capsys.readouterr().out
    assert "applied: no" in out
    assert "cloud (survives local daemon sleeping)" in out


def test_create_apply_calls_post_schedules_with_built_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(
        [
            "schedule",
            "create",
            "pod1",
            "--time",
            "22:30",
            "--days",
            "MON,TUE",
            "--apply",
        ]
    )

    assert rc == 0
    calls = _write_calls(fake, "post_schedules")
    assert len(calls) == 1
    _, pod_id, body = calls[0]
    assert pod_id == "pod1"
    assert body["when"] == {"time": "22:30", "weekDays": ["MON", "TUE"]}
    assert body["acState"]["on"] is True


def test_create_raw_body_overrides_friendly_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    raw = json.dumps({"acState": {"on": False}, "when": {"time": "06:00", "weekDays": ["SUN"]}})
    rc = main(["schedule", "create", "pod1", "--raw-body", raw, "--apply"])

    assert rc == 0
    calls = _write_calls(fake, "post_schedules")
    assert len(calls) == 1
    assert calls[0][2] == json.loads(raw)


def test_create_state_on_carries_mode_temperature_and_fan_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(
        [
            "schedule",
            "create",
            "pod1",
            "--time",
            "22:30",
            "--state",
            "on",
            "--mode",
            "cool",
            "--target-temperature",
            "22",
            "--fan-level",
            "medium",
            "--apply",
        ]
    )

    assert rc == 0
    calls = _write_calls(fake, "post_schedules")
    assert calls[0][2]["acState"] == {
        "on": True,
        "mode": "cool",
        "targetTemperature": 22,
        "fanLevel": "medium",
    }


def test_create_missing_time_is_a_user_error(capsys: pytest.CaptureFixture[str]) -> None:
    # A validation CliError raised inside cmd_create is caught by _dispatch and
    # returned as an int — not a SystemExit (that's argparse's own error path).
    rc = main(["schedule", "create", "pod1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


def test_create_invalid_time_is_a_user_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["schedule", "create", "pod1", "--time", "not-a-time"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "hint:" in err


def test_create_invalid_days_is_a_user_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["schedule", "create", "pod1", "--time", "10:00", "--days", "FUNDAY"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "hint:" in err


# --- delete: dry-run vs --apply -------------------------------------------


def test_delete_dry_run_makes_zero_write_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient(schedules={"result": [{"id": "sched1", "when": {"time": "08:00"}}]})
    _install_fake(monkeypatch, fake)

    rc = main(["schedule", "delete", "pod1", "sched1"])

    assert rc == 0
    assert _write_calls(fake, "delete_schedule") == []
    out = capsys.readouterr().out
    assert "sched1" in out
    assert "applied: no" in out
    assert "cloud (survives local daemon sleeping)" in out


def test_delete_apply_calls_delete_schedule_with_pod_and_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(schedules={"result": [{"id": "sched1"}]})
    _install_fake(monkeypatch, fake)

    rc = main(["schedule", "delete", "pod1", "sched1", "--apply"])

    assert rc == 0
    assert _write_calls(fake, "delete_schedule") == [("delete_schedule", "pod1", "sched1")]


def test_delete_json_carries_cloud_execution_marker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient(schedules={"result": []})
    _install_fake(monkeypatch, fake)

    rc = main(["schedule", "delete", "pod1", "sched1", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution"] == "cloud (survives local daemon sleeping)"
    assert payload["schedule_id"] == "sched1"


# --- ApiError bubbling ------------------------------------------------------


class _RaisingClient:
    def get_schedules(self, pod_id: str) -> object:
        raise HttpError(message="server exploded", status=500, remediation="retry later")


def test_list_api_error_maps_to_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule_module, "SensiboClient", lambda *a, **kw: _RaisingClient())
    rc = main(["schedule", "list", "pod1"])
    assert rc == EXIT_ENV_ERROR


def test_create_api_error_maps_to_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule_module, "SensiboClient", lambda *a, **kw: _RaisingClient())
    rc = main(["schedule", "create", "pod1", "--time", "10:00"])
    assert rc == EXIT_ENV_ERROR


def test_delete_api_error_maps_to_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule_module, "SensiboClient", lambda *a, **kw: _RaisingClient())
    rc = main(["schedule", "delete", "pod1", "sched1"])
    assert rc == EXIT_ENV_ERROR


# --- overview -------------------------------------------------------------


def test_schedule_overview_exists(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["schedule", "overview"])
    assert rc == 0
    assert "sensibo schedule" in capsys.readouterr().out


# --- explain ---------------------------------------------------------------


def test_schedule_explain_entries_resolve(capsys: pytest.CaptureFixture[str]) -> None:
    for path in (
        ["schedule"],
        ["schedule", "list"],
        ["schedule", "create"],
        ["schedule", "delete"],
    ):
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        capsys.readouterr()
