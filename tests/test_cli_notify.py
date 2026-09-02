"""Tests for ``sensibo notify test`` (task t6).

Written test-first: these fail against a CLI with no ``notify`` noun.

Dry-run by default: without ``--apply``, ``sensibo notify send`` never calls
:func:`sensibo.notify.send`. With ``--apply`` it calls it exactly once.
"""

from __future__ import annotations

import json

import pytest

from sensibo.cli import main
from sensibo.health import EXECUTION_LOCAL
from sensibo.notify import Outcome
from sensibo.notify._config import SCRIPT_VAR, WEBHOOK_VAR


@pytest.fixture(autouse=True)
def _no_real_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Point the ``~/.sensibo/.env`` fallback at an empty tmp dir.

    Otherwise a real operator ``.env`` on the test machine could leak a real
    webhook/script path into these tests.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(WEBHOOK_VAR, raising=False)
    monkeypatch.delenv(SCRIPT_VAR, raising=False)


# --- no transport configured ------------------------------------------------


def test_dry_run_with_nothing_configured_says_so_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["notify", "test"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not configured" in out.lower()


def test_apply_with_nothing_configured_is_a_user_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["notify", "test", "--apply"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert WEBHOOK_VAR in err
    assert SCRIPT_VAR in err


# --- dry-run never calls send ------------------------------------------------


def test_dry_run_never_calls_send(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(WEBHOOK_VAR, "https://example.invalid/hook")
    calls = []

    def _fake_send(payload, config):
        calls.append((payload, config))
        return [Outcome("webhook", True, "delivered")]

    monkeypatch.setattr("sensibo.notify.send", _fake_send)

    rc = main(["notify", "test"])
    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "would notify" in out.lower() or "dry" in out.lower()
    assert "https://example.invalid/hook" not in out  # webhook redacted


def test_dry_run_json_never_calls_send(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(WEBHOOK_VAR, "https://example.invalid/hook")
    calls = []
    monkeypatch.setattr(
        "sensibo.notify.send",
        lambda payload, config: calls.append((payload, config)) or [],
    )

    rc = main(["notify", "test", "--json"])
    assert rc == 0
    assert calls == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is False
    assert payload["sent"] is False
    assert payload["outcomes"] == []
    assert payload["payload"]["kind"] == "test"
    assert payload["execution"] == EXECUTION_LOCAL
    assert "https://example.invalid/hook" not in json.dumps(payload)


# --- --apply calls send exactly once ----------------------------------------


def test_apply_calls_send_exactly_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(WEBHOOK_VAR, "https://example.invalid/hook")
    calls = []

    def _fake_send(payload, config):
        calls.append((payload, config))
        return [Outcome("webhook", True, "delivered")]

    monkeypatch.setattr("sensibo.notify.send", _fake_send)

    rc = main(["notify", "test", "--apply"])
    assert rc == 0
    assert len(calls) == 1
    payload, config = calls[0]
    assert payload.kind == "test"

    out = capsys.readouterr().out
    assert "webhook" in out
    assert "ok" in out.lower()


def test_apply_json_calls_send_exactly_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(WEBHOOK_VAR, "https://example.invalid/hook")
    calls = []

    def _fake_send(payload, config):
        calls.append((payload, config))
        return [Outcome("webhook", True, "delivered")]

    monkeypatch.setattr("sensibo.notify.send", _fake_send)

    rc = main(["notify", "test", "--apply", "--json"])
    assert rc == 0
    assert len(calls) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is True
    assert payload["sent"] is True
    assert len(payload["outcomes"]) == 1
    assert payload["outcomes"][0]["transport"] == "webhook"
    assert payload["outcomes"][0]["ok"] is True
    assert payload["execution"] == EXECUTION_LOCAL


def test_apply_never_touches_urlopen_or_subprocess(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Belt-and-suspenders: even with a real transport configured, --apply must
    go through exactly one urlopen call (mocked), never more."""
    monkeypatch.setenv(WEBHOOK_VAR, "https://example.invalid/hook")

    calls = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(request, timeout=None):
        calls.append(request)
        return _FakeResponse()

    monkeypatch.setattr("sensibo.notify.transport.urllib.request.urlopen", _fake_urlopen)

    rc = main(["notify", "test", "--apply"])
    assert rc == 0
    assert len(calls) == 1


# --- execution marker ---------------------------------------------------


def test_execution_marker_present_in_text_and_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["notify", "test"])
    assert rc == 0
    assert EXECUTION_LOCAL in capsys.readouterr().out

    rc = main(["notify", "test", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution"] == EXECUTION_LOCAL
