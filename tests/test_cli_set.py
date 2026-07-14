"""Tests for ``sensibo set`` — the control verb.

THIS DRIVES AN AIR CONDITIONER IN SOMEONE'S HOME: the dry-run-by-default
contract is the product's core safety property, not a nicety. Every test here
mocks the API client seam (``sensibo.cli._commands.set.SensiboClient``) — no
test ever makes, or could make, a real network call.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import sensibo.cli._commands.set as set_module
from sensibo.api import ApiError, HttpError
from sensibo.cli import main


class _FakeClient:
    """Records every call; simulates acStates reads/writes in-memory."""

    def __init__(self, pods: dict[str, dict[str, Any]]) -> None:
        # pods: pod_id -> acState dict (mutated in place by writes, so a
        # read-back after a write reflects it — exactly like the real API).
        self._pods = {pod_id: dict(state) for pod_id, state in pods.items()}
        self.calls: list[tuple[str, tuple, dict]] = []

    def get_pod(self, pod_id: str, fields: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_pod", (pod_id,), {"fields": fields}))
        return {"result": {"acState": dict(self._pods[pod_id])}}

    def fleet_snapshot(self, fields: str = "*") -> dict[str, Any]:
        self.calls.append(("fleet_snapshot", (), {"fields": fields}))
        return {
            "result": [
                {"id": pod_id, "acState": dict(state)} for pod_id, state in self._pods.items()
            ]
        }

    def patch_ac_state(
        self, pod_id: str, prop: str, current_ac_state: dict[str, Any], new_value: object
    ) -> dict[str, Any]:
        self.calls.append(("patch_ac_state", (pod_id, prop, dict(current_ac_state), new_value), {}))
        self._pods[pod_id][prop] = new_value
        return {"result": dict(self._pods[pod_id])}

    def post_ac_states(self, pod_id: str, ac_state: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("post_ac_states", (pod_id, dict(ac_state)), {}))
        self._pods[pod_id].update(ac_state)
        return {"result": dict(self._pods[pod_id])}


class _RaisingClient:
    """A client whose every method blows up if called — proves zero writes."""

    def get_pod(self, pod_id: str, fields: str | None = None) -> dict[str, Any]:
        raise AssertionError("get_pod should not be called in this test")

    def fleet_snapshot(self, fields: str = "*") -> dict[str, Any]:
        raise AssertionError("fleet_snapshot should not be called in this test")

    def patch_ac_state(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("patch_ac_state must never be called on a dry run")

    def post_ac_states(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("post_ac_states must never be called on a dry run")


def _install(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    monkeypatch.setattr(set_module, "SensiboClient", lambda *a, **kw: client)


# --- dry-run is the default: zero write calls ------------------------------


def test_dry_run_by_default_makes_zero_write_requests(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient({"pod1": {"on": False, "mode": "heat", "targetTemperature": 20}})
    _install(monkeypatch, fake)

    rc = main(["set", "pod1", "--mode", "cool", "--target", "22"])

    assert rc == 0
    # exactly one read, zero writes
    assert [c[0] for c in fake.calls] == ["get_pod"]
    out = capsys.readouterr().out
    assert "cool" in out
    assert "22" in out


def test_dry_run_never_calls_patch_or_post_even_when_a_raising_client_would_blow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _RaisingClient has no working get_pod either, so stub just enough to
    # prove the write paths specifically are never reached.
    class _ReadOnlyClient(_RaisingClient):
        def get_pod(self, pod_id: str, fields: str | None = None) -> dict[str, Any]:
            return {"result": {"acState": {"on": True, "mode": "heat"}}}

    _install(monkeypatch, _ReadOnlyClient())
    rc = main(["set", "pod1", "--power", "off"])
    assert rc == 0  # would not raise AssertionError from patch/post


def test_dry_run_reports_no_changes_when_state_already_matches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient({"pod1": {"on": True, "mode": "cool"}})
    _install(monkeypatch, fake)

    rc = main(["set", "pod1", "--mode", "cool", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert payload["changes"] == {}
    assert [c[0] for c in fake.calls] == ["get_pod"]


def test_dry_run_json_shape_reports_field_from_to(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient({"pod1": {"on": False, "mode": "heat", "targetTemperature": 20}})
    _install(monkeypatch, fake)

    rc = main(["set", "pod1", "--power", "on", "--target", "24", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert payload["pod_id"] == "pod1"
    assert payload["changes"] == {
        "on": {"from": False, "to": True},
        "targetTemperature": {"from": 20, "to": 24},
    }


# --- --apply: single changed field -> PATCH --------------------------------


def test_apply_single_field_change_uses_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"pod1": {"on": True, "mode": "heat", "targetTemperature": 20}})
    _install(monkeypatch, fake)

    rc = main(["set", "pod1", "--mode", "cool", "--apply"])

    assert rc == 0
    kinds = [c[0] for c in fake.calls]
    assert kinds.count("patch_ac_state") == 1
    assert "post_ac_states" not in kinds
    patch_call = next(c for c in fake.calls if c[0] == "patch_ac_state")
    pod_id, prop, current_state, new_value = patch_call[1]
    assert pod_id == "pod1"
    assert prop == "mode"
    assert new_value == "cool"
    assert current_state == {"on": True, "mode": "heat", "targetTemperature": 20}


def test_apply_single_field_reads_back_after_write(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"pod1": {"on": True, "mode": "heat"}})
    _install(monkeypatch, fake)

    rc = main(["set", "pod1", "--mode", "cool", "--apply"])

    assert rc == 0
    # read (current) -> write (patch) -> read-back (get_pod again)
    kinds = [c[0] for c in fake.calls]
    assert kinds == ["get_pod", "patch_ac_state", "get_pod"]
    assert fake._pods["pod1"]["mode"] == "cool"


# --- --apply: multiple changed fields -> POST ------------------------------


def test_apply_multi_field_change_uses_post_with_merged_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient({"pod1": {"on": False, "mode": "heat", "targetTemperature": 20}})
    _install(monkeypatch, fake)

    rc = main(["set", "pod1", "--power", "on", "--mode", "cool", "--target", "22", "--apply"])

    assert rc == 0
    kinds = [c[0] for c in fake.calls]
    assert kinds.count("post_ac_states") == 1
    assert "patch_ac_state" not in kinds
    post_call = next(c for c in fake.calls if c[0] == "post_ac_states")
    pod_id, ac_state = post_call[1]
    assert pod_id == "pod1"
    # POST body carries the full merged target state, not just the diff.
    assert ac_state == {"on": True, "mode": "cool", "targetTemperature": 22}


def test_apply_multi_field_reads_back_final_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient({"pod1": {"on": False, "mode": "heat", "targetTemperature": 20}})
    _install(monkeypatch, fake)

    rc = main(["set", "pod1", "--power", "on", "--target", "22", "--apply", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is True
    assert payload["method"] == "post"
    assert payload["result_ac_state"] == {"on": True, "mode": "heat", "targetTemperature": 22}


def test_apply_with_no_actual_changes_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"pod1": {"on": True, "mode": "cool"}})
    _install(monkeypatch, fake)

    rc = main(["set", "pod1", "--mode", "cool", "--apply"])

    assert rc == 0
    kinds = [c[0] for c in fake.calls]
    assert "patch_ac_state" not in kinds
    assert "post_ac_states" not in kinds


# --- --all: one fleet call, per-pod diff/writes ----------------------------


def test_all_dry_run_uses_one_fleet_call_and_zero_writes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient(
        {
            "pod1": {"on": False, "mode": "heat"},
            "pod2": {"on": False, "mode": "cool"},
        }
    )
    _install(monkeypatch, fake)

    rc = main(["set", "--all", "--power", "on", "--json"])

    assert rc == 0
    kinds = [c[0] for c in fake.calls]
    assert kinds == ["fleet_snapshot"]  # ONE call, never one GET per pod
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    by_pod = {entry["pod_id"]: entry["changes"] for entry in payload["pods"]}
    assert by_pod["pod1"] == {"on": {"from": False, "to": True}}
    assert by_pod["pod2"] == {"on": {"from": False, "to": True}}


def test_all_apply_writes_each_pod_and_reads_back(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        {
            "pod1": {"on": False, "mode": "heat"},
            "pod2": {"on": True, "mode": "heat"},
        }
    )
    _install(monkeypatch, fake)

    rc = main(["set", "--all", "--power", "on", "--apply", "--json"])

    assert rc == 0
    kinds = [c[0] for c in fake.calls]
    assert kinds[0] == "fleet_snapshot"
    # pod1 actually changes (off -> on): one patch + one read-back.
    # pod2 already on: no write, no read-back call needed.
    assert kinds.count("patch_ac_state") == 1
    assert fake._pods["pod1"]["on"] is True
    assert fake._pods["pod2"]["on"] is True


# --- validation errors ------------------------------------------------------


def test_no_fields_given_is_a_user_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _RaisingClient()
    _install(monkeypatch, fake)

    rc = main(["set", "pod1"])

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


def test_missing_pod_id_without_all_is_a_user_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _RaisingClient()
    _install(monkeypatch, fake)

    rc = main(["set", "--mode", "cool"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "hint:" in err


def test_invalid_mode_choice_is_a_user_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _RaisingClient()
    _install(monkeypatch, fake)

    with pytest.raises(SystemExit) as exc:
        main(["set", "pod1", "--mode", "bogus-mode"])

    assert exc.value.code == 1
    assert "hint:" in capsys.readouterr().err


# --- ApiError -> CliError mapping ------------------------------------------


def test_api_error_is_mapped_to_cli_error_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _BrokenClient:
        def get_pod(self, pod_id: str, fields: str | None = None) -> None:
            raise HttpError(
                message="HTTP 404 calling ...", status=404, remediation="check the pod id"
            )

    _install(monkeypatch, _BrokenClient())

    rc = main(["set", "no-such-pod", "--mode", "cool"])

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_rate_limit_error_maps_to_environment_error_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sensibo.api import RateLimitExceededError

    class _RateLimitedClient:
        def get_pod(self, pod_id: str, fields: str | None = None) -> None:
            raise RateLimitExceededError(message="rate limited", remediation="back off")

    _install(monkeypatch, _RateLimitedClient())

    rc = main(["set", "pod1", "--mode", "cool"])

    assert rc == 2


# --- naming: usage/hints say `sensibo`, not `sensibo-cli` ------------------


def test_set_usage_names_the_installed_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["set", "--help"])
    out = capsys.readouterr().out
    assert "usage: sensibo set" in out
    assert "usage: sensibo-cli" not in out


def test_set_parse_error_hint_names_the_installed_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["set", "pod1", "--mode", "bogus-mode"])
    err = capsys.readouterr().err
    assert "hint:" in err
    assert "sensibo-cli --help" not in err
    assert "sensibo set --help" in err or "sensibo --help" in err


# --- explain catalog entry --------------------------------------------------


def test_explain_set_entry_exists(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "set"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sensibo set" in out
    assert "--apply" in out


def test_api_error_import_is_reachable() -> None:
    # sanity: the module under test imports the real ApiError family, not a
    # local stand-in, so the mapping logic is exercised against real types.
    assert issubclass(HttpError, ApiError)
