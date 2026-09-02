"""Tests for ``sensibo report daily|weekly`` (task t7).

Written test-first: these fail against a CLI with no ``report`` noun.

Dry-run by default (no ``--out``, no ``--apply``): nothing is written and
:func:`sensibo.notify.send` is never called. ``--out PATH`` writes a copy
without applying. ``--apply`` writes into the reports directory and delivers
exactly once.

Every test isolates ``HOME`` to a ``tmp_path`` and points ``SENSIBO_DB`` /
``SENSIBO_REPORTS_DIR`` explicitly, so nothing here ever touches a real
operator's ``~/.sensibo``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sensibo.cli import main
from sensibo.health import EXECUTION_LOCAL
from sensibo.notify import Outcome
from sensibo.notify._config import SCRIPT_VAR, WEBHOOK_VAR
from sensibo.store import Store


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point every path this verb touches at a throwaway tmp dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SENSIBO_DB", str(tmp_path / "sensibo.db"))
    monkeypatch.setenv("SENSIBO_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.delenv(WEBHOOK_VAR, raising=False)
    monkeypatch.delenv(SCRIPT_VAR, raising=False)
    monkeypatch.delenv("SENSIBO_DASHBOARD_URL", raising=False)


@pytest.fixture
def _seeded_store(tmp_path: Path) -> None:
    """A store with at least one location so render_report has something to draw."""
    store = Store(db_path=tmp_path / "sensibo.db")
    store.upsert_location("pod-1", kind="pod", product_model="airq", room_name="Office")
    store.record_reading("pod-1", "temperature", 21.5)
    store.close()


# --- dry-run by default -------------------------------------------------------


def test_dry_run_writes_nothing_and_never_calls_send(
    _seeded_store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    calls = []
    monkeypatch.setattr(
        "sensibo.report.deliver.send", lambda payload, config: calls.append(payload) or []
    )

    rc = main(["report", "daily"])
    assert rc == 0
    assert calls == []
    assert not (tmp_path / "reports").exists()

    out = capsys.readouterr().out
    assert "would write to" in out.lower()


def test_dry_run_json_shape(_seeded_store, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["report", "daily", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "daily"
    assert payload["window_hours"] == 24
    assert payload["apply"] is False
    assert payload["written_to"] is None
    assert payload["delivered"] is False
    assert payload["transports"] == []
    assert payload["outcomes"] == []
    assert payload["execution"] == EXECUTION_LOCAL


def test_weekly_dry_run_json_window_is_168(
    _seeded_store, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["report", "weekly", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "weekly"
    assert payload["window_hours"] == 168


# --- --out writes without --apply ---------------------------------------------


def test_out_writes_svg_without_apply_or_sending(
    _seeded_store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    calls = []
    monkeypatch.setattr(
        "sensibo.report.deliver.send", lambda payload, config: calls.append(payload) or []
    )

    out_path = tmp_path / "out.svg"
    rc = main(["report", "daily", "--out", str(out_path)])
    assert rc == 0
    assert calls == []
    assert out_path.exists()
    assert "<svg" in out_path.read_text(encoding="utf-8")
    assert not (tmp_path / "reports").exists()  # apply-only dir untouched


# --- --apply writes to reports dir and delivers exactly once -----------------


def test_apply_writes_to_reports_dir_mode_0700_and_delivers_once(
    _seeded_store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv(WEBHOOK_VAR, "https://example.invalid/hook")
    calls = []

    def _fake_send(payload, config):
        calls.append((payload, config))
        return [Outcome("webhook", True, "delivered")]

    monkeypatch.setattr("sensibo.report.deliver.send", _fake_send)

    rc = main(["report", "daily", "--apply"])
    assert rc == 0
    assert len(calls) == 1
    payload, _config = calls[0]
    assert payload.kind == "report"
    assert not hasattr(payload, "files")  # never a multipart upload

    reports_dir = tmp_path / "reports"
    assert reports_dir.is_dir()
    assert oct(reports_dir.stat().st_mode & 0o777) == oct(0o700)
    written = list(reports_dir.glob("daily-*.svg"))
    assert len(written) == 1

    out = capsys.readouterr().out
    assert "delivered to 1 transport" in out


def test_apply_json_shape(
    _seeded_store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(WEBHOOK_VAR, "https://example.invalid/hook")
    monkeypatch.setattr(
        "sensibo.report.deliver.send",
        lambda payload, config: [Outcome("webhook", True, "delivered")],
    )

    rc = main(["report", "weekly", "--apply", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is True
    assert payload["delivered"] is True
    assert payload["written_to"] is not None
    assert "weekly-" in payload["written_to"]
    assert len(payload["outcomes"]) == 1
    assert payload["outcomes"][0]["transport"] == "webhook"
    assert payload["outcomes"][0]["ok"] is True
    assert payload["execution"] == EXECUTION_LOCAL


def test_apply_all_transports_failing_raises_and_does_not_advance_meta(
    _seeded_store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """Q5: a configured transport that fails on every leg must not be
    reported as delivered, and must not advance the scheduling meta -- the
    next `sensibo report daily --apply` should still find it due."""
    monkeypatch.setenv(WEBHOOK_VAR, "https://example.invalid/hook/super-secret")
    monkeypatch.setattr(
        "sensibo.report.deliver.send",
        lambda payload, config: [Outcome("webhook", False, "HTTP 500")],
    )

    rc = main(["report", "daily", "--apply"])
    assert rc == 2

    store = Store(db_path=tmp_path / "sensibo.db")
    try:
        assert store.get_meta("last_daily_report_at") is None
    finally:
        store.close()

    # The failed transport's redacted detail (never the raw webhook URL)
    # appears in the CliError diagnostic on stderr.
    stderr = capsys.readouterr().err
    assert "example.invalid" not in stderr
    assert "webhook" in stderr
    assert "SENSIBO_NOTIFY_WEBHOOK" in stderr


def test_apply_all_transports_failing_json_mode_emits_standard_error_shape(
    _seeded_store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(WEBHOOK_VAR, "https://example.invalid/hook")
    monkeypatch.setattr(
        "sensibo.report.deliver.send",
        lambda payload, config: [Outcome("webhook", False, "HTTP 500")],
    )

    rc = main(["report", "daily", "--apply", "--json"])
    assert rc == 2
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == 2
    assert "webhook" in err["message"]


def test_apply_partial_delivery_success_counts_as_delivered(
    _seeded_store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One failing transport alongside one that succeeds still counts as
    delivered=True and still advances the meta."""
    monkeypatch.setenv(WEBHOOK_VAR, "https://example.invalid/hook")
    monkeypatch.setenv(SCRIPT_VAR, str(tmp_path / "notify.sh"))
    monkeypatch.setattr(
        "sensibo.report.deliver.send",
        lambda payload, config: [
            Outcome("webhook", False, "HTTP 500"),
            Outcome("script", True, "delivered"),
        ],
    )

    rc = main(["report", "daily", "--apply", "--json"])
    assert rc == 0
    store = Store(db_path=tmp_path / "sensibo.db")
    try:
        assert store.get_meta("last_daily_report_at") is not None
    finally:
        store.close()


def test_apply_records_meta_so_daemon_scheduler_sees_it_as_sent(
    _seeded_store, tmp_path: Path
) -> None:
    rc = main(["report", "daily", "--apply"])
    assert rc == 0

    store = Store(db_path=tmp_path / "sensibo.db")
    try:
        assert store.get_meta("last_daily_report_at") is not None
    finally:
        store.close()


def test_dashboard_url_appears_in_delivered_message_when_configured(
    _seeded_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(WEBHOOK_VAR, "https://example.invalid/hook")
    monkeypatch.setenv("SENSIBO_DASHBOARD_URL", "https://dash.example")
    calls = []

    def _fake_send(payload, config):
        calls.append(payload)
        return [Outcome("webhook", True, "delivered")]

    monkeypatch.setattr("sensibo.report.deliver.send", _fake_send)

    rc = main(["report", "daily", "--apply"])
    assert rc == 0
    assert "https://dash.example/reports/daily-" in calls[0].message


# --- explain / overview --------------------------------------------------------


def test_explain_report_daily_and_weekly(capsys: pytest.CaptureFixture[str]) -> None:
    for path in (["report"], ["report", "daily"], ["report", "weekly"]):
        rc = main(["explain", *path])
        assert rc == 0
        out = capsys.readouterr().out
        assert "sensibo report" in out.lower()


def test_report_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["report", "overview"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "report" in out.lower()


def test_execution_marker_present_in_text_and_json(
    _seeded_store, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["report", "daily"])
    assert rc == 0
    assert EXECUTION_LOCAL in capsys.readouterr().out

    rc = main(["report", "daily", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution"] == EXECUTION_LOCAL
