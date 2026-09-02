"""Tests for sensibo.report.schedule — report scheduling (task t7).

Written first (TDD): these fail against an empty ``sensibo/report/schedule.py``
and pass once :class:`~sensibo.report.schedule.ReportSchedule` and
:func:`~sensibo.report.schedule.due_reports` exist.

``due_reports`` decides, from the schedule and the last-sent instant, which of
``"daily"``/``"weekly"`` are due right now. It is pure and clock-injectable —
every test passes an explicit ``tz`` (UTC) and ``now`` so results never depend
on the host's timezone or the wall clock.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from sensibo.notify import NotifyConfig, Outcome
from sensibo.report.deliver import (
    build_payload,
    deliver_report,
    report_filename,
    reports_dir,
    resolve_dashboard_url,
    run_due_reports,
    write_report,
)
from sensibo.report.schedule import (
    DAILY,
    META_LAST_DAILY,
    META_LAST_WEEKLY,
    WEEKLY,
    ReportSchedule,
    due_reports,
)
from sensibo.store import Store

UTC = datetime.timezone.utc


def _epoch(y: int, m: int, d: int, hh: int, mm: int) -> float:
    return datetime.datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp()


# --- ReportSchedule: defaults and construction ------------------------------


def test_defaults() -> None:
    s = ReportSchedule()
    assert s.daily_at == "07:00"
    assert s.weekly_at == "07:00"
    assert s.weekly_day == 0


def test_is_frozen() -> None:
    s = ReportSchedule()
    with pytest.raises(Exception):
        s.daily_at = "08:00"  # type: ignore[misc]


# --- ReportSchedule.from_env -------------------------------------------------


def test_from_env_reads_all_three_vars() -> None:
    env = {
        "SENSIBO_REPORT_DAILY_AT": "06:30",
        "SENSIBO_REPORT_WEEKLY_AT": "08:15",
        "SENSIBO_REPORT_WEEKLY_DAY": "2",
    }
    s = ReportSchedule.from_env(env)
    assert s.daily_at == "06:30"
    assert s.weekly_at == "08:15"
    assert s.weekly_day == 2


def test_from_env_defaults_when_unset() -> None:
    assert ReportSchedule.from_env({}) == ReportSchedule()


@pytest.mark.parametrize("bad", ["7am", "24:00", "07:60", "-1:00", "", "7:00:00"])
def test_bad_daily_at_raises_value_error(bad: str) -> None:
    with pytest.raises(ValueError):
        ReportSchedule(daily_at=bad)


@pytest.mark.parametrize("bad", ["7am", "24:00"])
def test_bad_weekly_at_raises_value_error(bad: str) -> None:
    with pytest.raises(ValueError):
        ReportSchedule(weekly_at=bad)


@pytest.mark.parametrize("bad", [-1, 7, 100])
def test_bad_weekly_day_raises_value_error(bad: int) -> None:
    with pytest.raises(ValueError):
        ReportSchedule(weekly_day=bad)


def test_from_env_bad_weekly_day_raises_value_error() -> None:
    with pytest.raises(ValueError):
        ReportSchedule.from_env({"SENSIBO_REPORT_WEEKLY_DAY": "not-a-number"})


def test_from_env_bad_daily_at_raises_value_error() -> None:
    with pytest.raises(ValueError):
        ReportSchedule.from_env({"SENSIBO_REPORT_DAILY_AT": "not-a-time"})


# --- due_reports: daily -------------------------------------------------------


def test_daily_due_when_never_sent() -> None:
    s = ReportSchedule()
    now = _epoch(2026, 9, 2, 7, 1)  # Wednesday 07:01
    assert DAILY in due_reports(s, now, None, None, tz=UTC)


def test_daily_not_due_before_scheduled_time_today() -> None:
    s = ReportSchedule()
    last = _epoch(2026, 9, 1, 7, 0)  # yesterday's instant already sent
    now = _epoch(2026, 9, 2, 6, 59)
    assert DAILY not in due_reports(s, now, last, None, tz=UTC)


def test_daily_due_at_and_after_scheduled_time() -> None:
    s = ReportSchedule()
    last = _epoch(2026, 9, 1, 7, 0)
    now = _epoch(2026, 9, 2, 7, 0)
    assert DAILY in due_reports(s, now, last, None, tz=UTC)

    now = _epoch(2026, 9, 2, 7, 1)
    assert DAILY in due_reports(s, now, last, None, tz=UTC)


def test_daily_restart_at_659_then_701_yields_exactly_one_due_report() -> None:
    """Two scheduler restarts around the 07:00 boundary send exactly one report."""
    s = ReportSchedule()
    last = _epoch(2026, 9, 1, 7, 0)  # last sent = yesterday's instant

    now_659 = _epoch(2026, 9, 2, 6, 59)
    due_before = due_reports(s, now_659, last, None, tz=UTC)
    assert due_before.count(DAILY) == 0

    now_701 = _epoch(2026, 9, 2, 7, 1)
    due_after = due_reports(s, now_701, last, None, tz=UTC)
    assert due_after.count(DAILY) == 1


def test_daemon_down_across_two_due_instants_gets_at_most_one_catchup() -> None:
    s = ReportSchedule()
    last = _epoch(2026, 8, 28, 7, 0)  # five days of missed 07:00s
    now = _epoch(2026, 9, 2, 9, 0)
    due = due_reports(s, now, last, None, tz=UTC)
    assert due.count(DAILY) == 1


def test_daily_configurable_time_of_day() -> None:
    s = ReportSchedule(daily_at="18:30")
    last = _epoch(2026, 9, 1, 18, 30)
    now_before = _epoch(2026, 9, 2, 18, 29)
    assert DAILY not in due_reports(s, now_before, last, None, tz=UTC)
    now_after = _epoch(2026, 9, 2, 18, 31)
    assert DAILY in due_reports(s, now_after, last, None, tz=UTC)


# --- due_reports: weekly -------------------------------------------------------


def test_weekly_due_on_scheduled_day_at_or_after_time() -> None:
    s = ReportSchedule()  # weekly_day=0 (Monday)
    last = _epoch(2026, 8, 24, 7, 0)  # previous Monday's instant
    now = _epoch(2026, 8, 31, 7, 1)  # this Monday, 07:01
    assert WEEKLY in due_reports(s, now, None, last, tz=UTC)


def test_weekly_not_due_midweek_after_scheduled_day_already_sent() -> None:
    s = ReportSchedule()
    last = _epoch(2026, 8, 31, 7, 0)  # this week's Monday already sent
    now = _epoch(2026, 9, 2, 12, 0)  # Wednesday
    assert WEEKLY not in due_reports(s, now, None, last, tz=UTC)


def test_weekly_due_when_never_sent() -> None:
    s = ReportSchedule()
    now = _epoch(2026, 9, 2, 12, 0)  # Wednesday, well past this week's Monday
    assert WEEKLY in due_reports(s, now, None, None, tz=UTC)


def test_weekly_configurable_day() -> None:
    s = ReportSchedule(weekly_day=2)  # Wednesday
    last = _epoch(2026, 8, 26, 7, 0)  # previous Wednesday's instant
    now = _epoch(2026, 9, 2, 7, 1)  # this Wednesday, after 07:00
    assert WEEKLY in due_reports(s, now, None, last, tz=UTC)


def test_weekly_down_across_multiple_weeks_gets_at_most_one_catchup() -> None:
    s = ReportSchedule()
    last = _epoch(2026, 8, 3, 7, 0)  # several Mondays ago
    now = _epoch(2026, 8, 31, 9, 0)
    due = due_reports(s, now, None, last, tz=UTC)
    assert due.count(WEEKLY) == 1


# --- both together -------------------------------------------------------------


def test_both_daily_and_weekly_can_be_due_together() -> None:
    s = ReportSchedule()
    now = _epoch(2026, 8, 31, 7, 1)  # Monday, past both scheduled instants
    due = due_reports(s, now, None, None, tz=UTC)
    assert DAILY in due
    assert WEEKLY in due


def test_host_local_tz_default_is_used_when_tz_not_given() -> None:
    """``tz`` defaults to the host local zone; passing a fixed offset changes the answer."""
    s = ReportSchedule()
    now = _epoch(2026, 9, 2, 7, 1)
    plus_twelve = datetime.timezone(datetime.timedelta(hours=12))
    # At UTC+12 the same instant is already the next calendar day, past 07:00.
    due_plus_twelve = due_reports(s, now, None, None, tz=plus_twelve)
    assert DAILY in due_plus_twelve


# --- sensibo.report.deliver: reports_dir / write_report ----------------------

NOW = _epoch(2026, 9, 2, 7, 1)  # a Wednesday


def test_reports_dir_env_override(tmp_path: Path) -> None:
    override = tmp_path / "custom-reports"
    assert reports_dir({"SENSIBO_REPORTS_DIR": str(override)}) == override


def test_reports_dir_default_is_home_relative(tmp_path: Path) -> None:
    assert reports_dir({"HOME": str(tmp_path)}) == Path.home() / ".sensibo" / "reports"


def test_report_filename_daily_and_weekly() -> None:
    assert report_filename(DAILY, NOW) == "daily-2026-09-02.svg"
    assert report_filename(WEEKLY, NOW) == "weekly-2026-W36.svg"


def test_write_report_creates_dir_mode_0700_and_writes_svg(tmp_path: Path) -> None:
    target_dir = tmp_path / "reports"
    path = write_report(DAILY, "<svg>hello</svg>", NOW, target_dir)
    assert path == target_dir / "daily-2026-09-02.svg"
    assert path.read_text(encoding="utf-8") == "<svg>hello</svg>"
    assert oct(target_dir.stat().st_mode & 0o777) == oct(0o700)


# --- sensibo.report.deliver: build_payload / deliver_report -------------------


def test_build_payload_carries_path_and_no_dashboard_url() -> None:
    payload = build_payload(DAILY, Path("/tmp/x/daily-2026-09-02.svg"), None)
    assert payload.kind == "report"
    assert payload.status == DAILY
    assert "daily-2026-09-02.svg" in payload.message
    assert "http" not in payload.message


def test_build_payload_carries_dashboard_url_when_configured() -> None:
    payload = build_payload(DAILY, Path("/tmp/x/daily-2026-09-02.svg"), "https://dash.example")
    assert "https://dash.example/reports/daily-2026-09-02.svg" in payload.message


def test_resolve_dashboard_url_unset_is_none() -> None:
    assert resolve_dashboard_url({}) is None


def test_resolve_dashboard_url_reads_env() -> None:
    assert resolve_dashboard_url({"SENSIBO_DASHBOARD_URL": "https://dash.example"}) == (
        "https://dash.example"
    )


def test_deliver_report_with_notifier_never_calls_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """No multipart upload: the notifier gets a Payload, never a files= kwarg."""
    calls: list[object] = []

    def _fake_notifier(payload):
        calls.append(payload)
        return [Outcome("webhook", True, "delivered")]

    def _boom_send(*_args, **_kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("sensibo.notify.send must not be called when notifier is given")

    monkeypatch.setattr("sensibo.report.deliver.send", _boom_send)

    outcomes = deliver_report(
        DAILY, Path("/tmp/x/daily-2026-09-02.svg"), NotifyConfig(), None, notifier=_fake_notifier
    )
    assert len(calls) == 1
    assert not hasattr(calls[0], "files")
    assert outcomes[0].ok is True


def test_deliver_report_without_notifier_calls_send(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def _fake_send(payload, config):
        calls.append((payload, config))
        return [Outcome("webhook", True, "delivered")]

    monkeypatch.setattr("sensibo.report.deliver.send", _fake_send)

    config = NotifyConfig(webhook_url="https://example.invalid/hook")
    outcomes = deliver_report(WEEKLY, Path("/tmp/x/weekly-2026-W36.svg"), config, None)
    assert len(calls) == 1
    payload, used_config = calls[0]
    assert payload.kind == "report"
    assert used_config is config
    assert outcomes[0].ok is True


# --- sensibo.report.deliver: run_due_reports ----------------------------------


@pytest.fixture()
def _store(tmp_path: Path) -> Store:
    s = Store(db_path=tmp_path / "t.db")
    yield s
    s.close()


def test_run_due_reports_writes_sets_meta_and_delivers(_store: Store, tmp_path: Path) -> None:
    calls: list[object] = []

    def _notifier(payload):
        calls.append(payload)
        return [Outcome("webhook", True, "delivered")]

    schedule = ReportSchedule()
    target_dir = tmp_path / "reports"
    runs = run_due_reports(_store, schedule, NotifyConfig(), NOW, _notifier, target_dir)

    kinds = {r.kind for r in runs}
    assert kinds == {DAILY, WEEKLY}
    for run in runs:
        assert run.path.exists()
        assert run.outcomes[0].ok is True
    assert len(calls) == 2

    assert _store.get_meta(META_LAST_DAILY) is not None
    assert _store.get_meta(META_LAST_WEEKLY) is not None


def test_run_due_reports_second_call_same_instant_sends_nothing_more(
    _store: Store, tmp_path: Path
) -> None:
    calls: list[object] = []

    def _notifier(payload):
        calls.append(payload)
        return [Outcome("webhook", True, "delivered")]

    schedule = ReportSchedule()
    target_dir = tmp_path / "reports"
    run_due_reports(_store, schedule, NotifyConfig(), NOW, _notifier, target_dir)
    assert len(calls) == 2

    # A second cycle at the same instant (nothing new due) sends nothing more.
    runs = run_due_reports(_store, schedule, NotifyConfig(), NOW, _notifier, target_dir)
    assert runs == []
    assert len(calls) == 2


def test_run_due_reports_never_raises_and_leaves_meta_unset_on_failure(
    _store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged: list[str] = []

    def _boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("sensibo.report.deliver.write_report", _boom)

    schedule = ReportSchedule()
    runs = run_due_reports(
        _store,
        schedule,
        NotifyConfig(),
        NOW,
        lambda payload: [Outcome("webhook", True, "delivered")],
        tmp_path / "reports",
        log=logged.append,
    )
    assert runs == []
    assert _store.get_meta(META_LAST_DAILY) is None
    assert _store.get_meta(META_LAST_WEEKLY) is None
    assert logged  # something was logged
