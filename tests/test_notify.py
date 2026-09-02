"""Tests for sensibo.notify: config resolution, webhook/script transport, redaction.

Hard rule: no test makes a real network call or spawns a real process, except
the script-delivery tests — which run a tiny throwaway shell script written
into ``tmp_path`` on purpose, because the child environment (no API key, no
webhook URL) and the stdin payload are only provable against a real child.
Webhook delivery is tested through the one seam the transport calls through:
``sensibo.notify.transport.urllib.request.urlopen``, monkeypatched with a fake
that records the built ``urllib.request.Request``. No test touches a real
``~/.sensibo`` file; every config test passes an explicit ``home`` (a pytest
``tmp_path``) and/or an ``env`` mapping.
"""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

from sensibo.notify._config import SCRIPT_VAR, WEBHOOK_VAR, resolve_notify_config
from sensibo.notify.transport import (
    REDACTED_WEBHOOK,
    TRANSPORT_NONE,
    TRANSPORT_SCRIPT,
    TRANSPORT_WEBHOOK,
    Outcome,
    Payload,
    redact,
    render_dry_run,
    send,
)

WEBHOOK_URL = "https://discord.com/api/webhooks/1234/secret-token"


def _payload(**overrides: Any) -> Payload:
    base = dict(
        kind="sensor_down",
        location="ms_o7dH4GeY",
        status="down",
        since="2026-09-02T05:52:00+00:00",
        last_ok="2026-09-02T05:51:09+00:00",
        message="spare-room sensor has been down since 05:52",
    )
    base.update(overrides)
    return Payload(**base)


class _FakeResponse:
    def __init__(self, code: int = 204) -> None:
        self.code = code

    def read(self) -> bytes:
        return b""

    def close(self) -> None:
        pass

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        pass


class _RecordingUrlopen:
    """Records the Request objects passed to it; raises canned errors if queued."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self._raise: Exception | None = None

    def set_error(self, error: Exception) -> None:
        self._raise = error

    def __call__(self, request: Any, timeout: float | None = None) -> _FakeResponse:
        self.calls.append(request)
        if self._raise is not None:
            raise self._raise
        return _FakeResponse()


# --- config resolution -------------------------------------------------------


def test_env_var_wins_when_present(tmp_path: Path) -> None:
    sensibo_dir = tmp_path / ".sensibo"
    sensibo_dir.mkdir()
    (sensibo_dir / ".env").write_text(f"{WEBHOOK_VAR}=from-dotenv\n", encoding="utf-8")

    config = resolve_notify_config(env={WEBHOOK_VAR: "from-env"}, home=tmp_path)
    assert config.webhook_url == "from-env"


def test_falls_back_to_dotenv_when_env_var_absent(tmp_path: Path) -> None:
    sensibo_dir = tmp_path / ".sensibo"
    sensibo_dir.mkdir()
    (sensibo_dir / ".env").write_text(
        f'{WEBHOOK_VAR}="{WEBHOOK_URL}"\n{SCRIPT_VAR}=/opt/hooks/alert.sh\n',
        encoding="utf-8",
    )

    config = resolve_notify_config(env={}, home=tmp_path)
    assert config.webhook_url == WEBHOOK_URL
    assert config.script_path == "/opt/hooks/alert.sh"
    assert config.timeout == 10.0


def test_empty_env_value_is_treated_as_absent(tmp_path: Path) -> None:
    sensibo_dir = tmp_path / ".sensibo"
    sensibo_dir.mkdir()
    (sensibo_dir / ".env").write_text(f"{WEBHOOK_VAR}=from-dotenv\n", encoding="utf-8")

    config = resolve_notify_config(env={WEBHOOK_VAR: ""}, home=tmp_path)
    assert config.webhook_url == "from-dotenv"


def test_unconfigured_returns_none_fields_and_default_timeout(tmp_path: Path) -> None:
    config = resolve_notify_config(env={}, home=tmp_path)
    assert config.webhook_url is None
    assert config.script_path is None
    assert config.timeout == 10.0


# --- payload ------------------------------------------------------------------


def test_payload_defaults_execution_to_the_local_marker() -> None:
    payload = _payload()
    assert payload.execution == "local (stops when this daemon stops)"


def test_payload_to_json_is_compact_and_carries_all_fields() -> None:
    payload = _payload()
    body = payload.to_json()
    assert ", " not in body and ": " not in body
    parsed = json.loads(body)
    assert parsed["kind"] == "sensor_down"
    assert parsed["location"] == "ms_o7dH4GeY"
    assert parsed["status"] == "down"
    assert parsed["since"] == payload.since
    assert parsed["last_ok"] == payload.last_ok
    assert parsed["message"] == payload.message
    assert parsed["execution"] == payload.execution


# --- webhook delivery ---------------------------------------------------------


def test_webhook_posts_json_with_content_type_header(monkeypatch: pytest.MonkeyPatch) -> None:
    import sensibo.notify.transport as transport_module

    fake = _RecordingUrlopen()
    monkeypatch.setattr(transport_module.urllib.request, "urlopen", fake)
    config = resolve_notify_config(env={WEBHOOK_VAR: WEBHOOK_URL}, home="/nonexistent-home")
    payload = _payload()

    outcomes = send(payload, config)

    assert len(fake.calls) == 1
    request = fake.calls[0]
    assert request.get_method() == "POST"
    assert request.full_url == WEBHOOK_URL
    assert request.headers.get("Content-type") == "application/json"
    assert json.loads(request.data.decode("utf-8"))["message"] == payload.message
    assert outcomes == [Outcome(TRANSPORT_WEBHOOK, True, "delivered")]


def test_webhook_http_500_returns_failed_outcome_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sensibo.notify.transport as transport_module

    fake = _RecordingUrlopen()
    fake.set_error(HTTPError(WEBHOOK_URL, 500, "Internal Server Error", {}, io.BytesIO(b"")))
    monkeypatch.setattr(transport_module.urllib.request, "urlopen", fake)
    config = resolve_notify_config(env={WEBHOOK_VAR: WEBHOOK_URL}, home="/nonexistent-home")

    outcomes = send(_payload(), config)

    assert outcomes == [Outcome(TRANSPORT_WEBHOOK, False, "HTTP 500")]


def test_webhook_network_error_returns_failed_outcome_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sensibo.notify.transport as transport_module

    fake = _RecordingUrlopen()
    fake.set_error(URLError("Name or service not known"))
    monkeypatch.setattr(transport_module.urllib.request, "urlopen", fake)
    config = resolve_notify_config(env={WEBHOOK_VAR: WEBHOOK_URL}, home="/nonexistent-home")

    outcomes = send(_payload(), config)

    assert outcomes == [
        Outcome(TRANSPORT_WEBHOOK, False, "network error: Name or service not known")
    ]


# --- script delivery ------------------------------------------------------------


def _write_hook_script(path: Path, dump_file: Path) -> Path:
    """A tiny real hook: dumps its stdin and env to ``dump_file`` and exits 0.

    The dump-file path is baked into the script text (the transport invokes the
    hook with a fixed one-element argv), so each test writes its own copy.
    """
    path.write_text(
        "#!/bin/sh\n" f"{{ printf 'STDIN:%s\\n' \"$(cat)\"; env; }} > {dump_file}\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_script_receives_payload_on_stdin_and_a_stripped_child_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dump_file = tmp_path / "dump.txt"
    script = _write_hook_script(tmp_path / "hook.sh", dump_file)
    # The parent process must actually carry both secrets for the assertion to mean anything.
    monkeypatch.setenv("SENSIBO_API_KEY", "sk-test-secret")  # noqa: S105 - test-only fixture value
    monkeypatch.setenv(WEBHOOK_VAR, WEBHOOK_URL)
    config = resolve_notify_config(env={SCRIPT_VAR: str(script)}, home="/nonexistent-home")
    payload = _payload()

    outcomes = send(payload, config)

    assert outcomes == [Outcome(TRANSPORT_SCRIPT, True, "delivered")]
    dump = dump_file.read_text(encoding="utf-8")
    lines = dump.splitlines()
    assert lines[0].startswith("STDIN:")
    assert json.loads(lines[0][len("STDIN:") :])["message"] == payload.message
    child_env_lines = {line.split("=", 1)[0] for line in lines[1:] if "=" in line}
    assert "SENSIBO_API_KEY" not in child_env_lines
    assert WEBHOOK_VAR not in child_env_lines
    # Sanity: the parent really had them (so the strip above did something).
    assert os.environ.get("SENSIBO_API_KEY") == "sk-test-secret"


def test_script_nonzero_exit_returns_failed_outcome(tmp_path: Path) -> None:
    script = tmp_path / "fail.sh"
    script.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    config = resolve_notify_config(env={SCRIPT_VAR: str(script)}, home="/nonexistent-home")

    outcomes = send(_payload(), config)

    assert outcomes == [Outcome(TRANSPORT_SCRIPT, False, "exit code 3")]


def test_script_timeout_returns_failed_outcome(tmp_path: Path) -> None:
    script = tmp_path / "sleep.sh"
    script.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    config = resolve_notify_config(env={SCRIPT_VAR: str(script)}, home="/nonexistent-home")
    config = type(config)(
        webhook_url=config.webhook_url, script_path=config.script_path, timeout=0.2
    )

    outcomes = send(_payload(), config)

    assert outcomes == [Outcome(TRANSPORT_SCRIPT, False, "timed out after 0.2s")]


# --- unconfigured / multi-transport --------------------------------------------


def test_send_with_no_transport_returns_not_configured_and_never_raises() -> None:
    config = resolve_notify_config(env={}, home="/nonexistent-home")
    outcomes = send(_payload(), config)
    assert outcomes == [Outcome(TRANSPORT_NONE, False, "not configured")]


def test_send_delivers_to_both_transports_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    import sensibo.notify.transport as transport_module

    fake = _RecordingUrlopen()
    monkeypatch.setattr(transport_module.urllib.request, "urlopen", fake)
    config = resolve_notify_config(
        env={WEBHOOK_VAR: WEBHOOK_URL, SCRIPT_VAR: "/bin/true"}, home="/nonexistent-home"
    )

    outcomes = send(_payload(), config)

    assert [outcome.transport for outcome in outcomes] == [TRANSPORT_WEBHOOK, TRANSPORT_SCRIPT]
    assert all(outcome.ok for outcome in outcomes)


# --- redaction / dry-run --------------------------------------------------------


def test_redact_replaces_every_occurrence_of_the_webhook_url() -> None:
    config = resolve_notify_config(env={WEBHOOK_VAR: WEBHOOK_URL}, home="/nonexistent-home")
    text = f"POST {WEBHOOK_URL} failed; retrying {WEBHOOK_URL}"
    assert config.webhook_url not in redact(text, config)
    assert REDACTED_WEBHOOK in redact(text, config)


def test_redact_is_a_noop_without_a_webhook_configured() -> None:
    config = resolve_notify_config(env={}, home="/nonexistent-home")
    assert redact("anything at all", config) == "anything at all"


def test_render_dry_run_redacts_the_url_and_shows_the_exact_message() -> None:
    config = resolve_notify_config(
        env={WEBHOOK_VAR: WEBHOOK_URL, SCRIPT_VAR: "/opt/hooks/alert.sh"}, home="/nonexistent-home"
    )
    payload = _payload(message="bedroom sensor battery low")
    rendered = render_dry_run(payload, config)

    assert WEBHOOK_URL not in rendered
    assert REDACTED_WEBHOOK in rendered
    assert "bedroom sensor battery low" in rendered
    assert "/opt/hooks/alert.sh" in rendered
    assert "local (stops when this daemon stops)" in rendered


def test_render_dry_run_makes_zero_urlopen_or_subprocess_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sensibo.notify.transport as transport_module

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("render_dry_run must not touch the network")

    def _explode_proc(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("render_dry_run must not spawn a subprocess")

    monkeypatch.setattr(transport_module.urllib.request, "urlopen", _explode)
    monkeypatch.setattr(transport_module.subprocess, "run", _explode_proc)
    config = resolve_notify_config(
        env={WEBHOOK_VAR: WEBHOOK_URL, SCRIPT_VAR: "/opt/hooks/alert.sh"}, home="/nonexistent-home"
    )

    rendered = render_dry_run(_payload(), config)

    assert isinstance(rendered, str)
    assert WEBHOOK_URL not in rendered
