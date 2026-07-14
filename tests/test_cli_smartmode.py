"""Tests for ``sensibo smartmode`` (Climate React) — task t8.

Every test mocks ``sensibo.cli._commands.smartmode.SensiboClient`` — never a
real network call, never a real ``~/.sensibo`` key. Each write proves ZERO
calls to the mutating client method without ``--apply``, and exactly the
expected call with it.
"""

from __future__ import annotations

import json

import pytest

import sensibo.cli._commands.smartmode as smartmode_module
from sensibo.cli import main


class _FakeClient:
    def __init__(self, smartmode: object | None = None) -> None:
        self.calls: list[tuple] = []
        self._smartmode = smartmode if smartmode is not None else {"enabled": False}

    def get_smartmode(self, pod_id: str) -> object:
        self.calls.append(("get_smartmode", pod_id))
        return self._smartmode

    def put_smartmode(self, pod_id: str, body: dict) -> object:
        self.calls.append(("put_smartmode", pod_id, body))
        return {"enabled": body["enabled"]}


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(smartmode_module, "SensiboClient", lambda *a, **kw: fake)


def _write_calls(fake: _FakeClient) -> list[tuple]:
    return [c for c in fake.calls if c[0] == "put_smartmode"]


# --- show -------------------------------------------------------------


def test_show_reads_through_the_mocked_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient(smartmode={"enabled": True, "type": "temperature"})
    _install_fake(monkeypatch, fake)

    rc = main(["smartmode", "show", "pod1"])

    assert rc == 0
    assert fake.calls == [("get_smartmode", "pod1")]
    out = capsys.readouterr().out
    assert "pod1" in out
    assert "cloud (survives local daemon sleeping)" in out


def test_show_json_carries_cloud_execution_marker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient(smartmode={"enabled": True})
    _install_fake(monkeypatch, fake)

    rc = main(["smartmode", "show", "pod1", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pod"] == "pod1"
    assert payload["execution"] == "cloud (survives local daemon sleeping)"


# --- enable/disable: dry-run vs --apply --------------------------------


def test_enable_dry_run_makes_zero_write_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(["smartmode", "enable", "pod1"])

    assert rc == 0
    assert _write_calls(fake) == []
    out = capsys.readouterr().out
    assert "applied: no" in out
    assert "cloud (survives local daemon sleeping)" in out


def test_enable_apply_calls_put_smartmode_with_enabled_true(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(["smartmode", "enable", "pod1", "--apply"])

    assert rc == 0
    assert _write_calls(fake) == [("put_smartmode", "pod1", {"enabled": True})]


def test_disable_dry_run_makes_zero_write_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(smartmode={"enabled": True})
    _install_fake(monkeypatch, fake)

    rc = main(["smartmode", "disable", "pod1"])

    assert rc == 0
    assert _write_calls(fake) == []


def test_disable_apply_calls_put_smartmode_with_enabled_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(smartmode={"enabled": True})
    _install_fake(monkeypatch, fake)

    rc = main(["smartmode", "disable", "pod1", "--apply"])

    assert rc == 0
    assert _write_calls(fake) == [("put_smartmode", "pod1", {"enabled": False})]


def test_enable_apply_json_carries_cloud_execution_marker_and_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeClient()
    _install_fake(monkeypatch, fake)

    rc = main(["smartmode", "enable", "pod1", "--apply", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is True
    assert payload["result"] == {"enabled": True}
    assert payload["execution"] == "cloud (survives local daemon sleeping)"


# --- overview -----------------------------------------------------------


def test_smartmode_overview_exists(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["smartmode", "overview"])
    assert rc == 0
    assert "sensibo smartmode" in capsys.readouterr().out


def test_bare_smartmode_noun_shows_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["smartmode"])
    assert rc == 0
    assert capsys.readouterr().out.strip()


# --- registration / explain -----------------------------------------------


def test_smartmode_explain_entries_resolve(capsys: pytest.CaptureFixture[str]) -> None:
    for path in (["smartmode"], ["smartmode", "show"], ["smartmode", "enable"]):
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        capsys.readouterr()
