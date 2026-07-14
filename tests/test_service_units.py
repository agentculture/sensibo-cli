"""Tests for :mod:`sensibo.service` — the always-on systemd units.

Two properties matter more than any other here, and they get the most tests:

1. **The dry-run contract is structural.** ``build_install_plan`` must not
   write a file or run a mutating command, ever — not "must not when a flag is
   unset", but must not, period. The only function that mutates is
   ``apply_install``.
2. **The units must actually be always-on.** ``Restart=always`` and
   ``WantedBy=default.target`` + lingering are not cosmetic: without them the
   collector dies on the first cloud blip or at logout, and the ~7-day cloud
   window turns that gap into permanently lost data.

No test here touches a real systemd: every subprocess call goes through an
injected fake runner, and every file write is rooted at ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sensibo.service import (
    COLLECT_UNIT,
    TARGET_UNIT,
    WEB_UNIT,
    RunResult,
    ServiceError,
    apply_install,
    apply_uninstall,
    build_install_plan,
    build_uninstall_plan,
    render_collect_unit,
    render_target,
    render_web_unit,
    resolve_exec_path,
    status,
)
from sensibo.service._units import exec_line

EXEC = "/opt/venv/bin/sensibo"


class FakeRunner:
    """Records argv, returns canned results. Never touches a real systemd."""

    def __init__(self, *, linger: bool = False, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self._linger = linger
        self._fail_on = fail_on

    def __call__(self, argv: list[str]) -> RunResult:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if self._fail_on and self._fail_on in joined:
            return RunResult(tuple(argv), returncode=1, stderr="boom")
        if "show-user" in argv:
            return RunResult(
                tuple(argv),
                returncode=0,
                stdout="Linger=yes\n" if self._linger else "Linger=no\n",
            )
        if "is-enabled" in argv:
            return RunResult(tuple(argv), returncode=0, stdout="enabled\n")
        if "is-active" in argv:
            return RunResult(tuple(argv), returncode=0, stdout="active\n")
        return RunResult(tuple(argv), returncode=0)


# --- unit rendering: the always-on properties ------------------------------


def test_collect_unit_restarts_always() -> None:
    """Restart=always is what makes the collector survive an ApiError exit.

    `collect --daemon` exits (code 2) on a cloud blip or a boot-time network
    race — see sensibo/cli/_commands/collect.py. systemd is the only supervisor
    that brings it back, and the ~7-day cloud history window means a gap it
    fails to recover is unrecoverable.
    """
    unit = render_collect_unit(EXEC, interval=60)
    assert "Restart=always" in unit.content
    assert "RestartSec=" in unit.content
    assert f"ExecStart={EXEC} collect --daemon --interval 60" in unit.content


def test_target_is_wanted_by_default_target() -> None:
    """WantedBy=default.target + lingering is the whole start-at-boot story."""
    assert "WantedBy=default.target" in render_target().content


def test_services_are_wanted_by_and_part_of_the_target() -> None:
    """WantedBy links them into the target on enable; PartOf stops them with it."""
    for unit in (
        render_collect_unit(EXEC, interval=60),
        render_web_unit(EXEC, bind="0.0.0.0:8323"),
    ):
        assert f"WantedBy={TARGET_UNIT}" in unit.content
        assert f"PartOf={TARGET_UNIT}" in unit.content


def test_no_unit_ever_names_the_api_key() -> None:
    """A unit file is world-readable; the key resolves inside the client instead."""
    for unit in (
        render_collect_unit(EXEC, interval=60),
        render_web_unit(EXEC, bind="0.0.0.0:8323"),
        render_target(),
    ):
        assert "EnvironmentFile" not in unit.content
        assert "Environment=" not in unit.content


def test_collect_unit_carries_db_override_when_given() -> None:
    unit = render_collect_unit(EXEC, interval=120, db="/srv/sensibo.db")
    assert f"ExecStart={EXEC} collect --daemon --interval 120 --db /srv/sensibo.db" in unit.content


def test_web_unit_carries_bind_and_token_file() -> None:
    unit = render_web_unit(EXEC, bind="127.0.0.1:9000", token_file="/srv/token")
    assert f"ExecStart={EXEC} web --bind 127.0.0.1:9000 --token-file /srv/token" in unit.content


def test_exec_line_quotes_paths_with_spaces() -> None:
    """systemd splits ExecStart on whitespace — an unquoted spacey path silently splits."""
    assert exec_line(["/home/some user/bin/sensibo", "web"]) == '"/home/some user/bin/sensibo" web'


# --- the dry-run contract is structural ------------------------------------


def test_build_install_plan_writes_nothing(tmp_path: Path) -> None:
    """The plan builder is pure. Not 'pure unless a flag is set' — pure."""
    runner = FakeRunner()
    plan = build_install_plan(exec_path=EXEC, unit_dir=tmp_path, runner=runner)

    assert list(tmp_path.iterdir()) == [], "build_install_plan must not write any file"
    assert len(plan.units) == 3
    # The only command it is allowed to run is the read-only linger *query*.
    for call in runner.calls:
        assert "show-user" in call, f"plan builder ran a mutating command: {call}"


def test_install_plan_enables_linger_when_absent(tmp_path: Path) -> None:
    plan = build_install_plan(exec_path=EXEC, unit_dir=tmp_path, runner=FakeRunner(linger=False))
    commands = [" ".join(c) for c in plan.commands]

    assert plan.linger_already_enabled is False
    assert any(c.startswith("loginctl enable-linger") for c in commands)


def test_install_plan_skips_linger_when_already_enabled(tmp_path: Path) -> None:
    """Re-enabling lingering is harmless but noisy — an honest plan says 'already done'."""
    plan = build_install_plan(exec_path=EXEC, unit_dir=tmp_path, runner=FakeRunner(linger=True))
    commands = [" ".join(c) for c in plan.commands]

    assert plan.linger_already_enabled is True
    assert not any("enable-linger" in c for c in commands)


def test_install_plan_enables_every_unit_it_writes(tmp_path: Path) -> None:
    """`enable sensibo.target` alone does NOT enable its members — each needs enabling."""
    plan = build_install_plan(exec_path=EXEC, unit_dir=tmp_path, runner=FakeRunner())
    enable = next(c for c in plan.commands if "enable" in c and "--now" in c)

    assert TARGET_UNIT in enable
    assert COLLECT_UNIT in enable
    assert WEB_UNIT in enable


def test_install_plan_can_omit_the_web_unit(tmp_path: Path) -> None:
    plan = build_install_plan(exec_path=EXEC, unit_dir=tmp_path, web=False, runner=FakeRunner())
    names = [u.name for u in plan.units]

    assert WEB_UNIT not in names
    assert COLLECT_UNIT in names


def test_install_plan_rejects_installing_nothing(tmp_path: Path) -> None:
    with pytest.raises(ServiceError) as excinfo:
        build_install_plan(
            exec_path=EXEC, unit_dir=tmp_path, collect=False, web=False, runner=FakeRunner()
        )
    assert "nothing to install" in excinfo.value.message
    assert excinfo.value.remediation


# --- apply: the one function allowed to mutate -----------------------------


def test_apply_install_writes_units_then_runs_commands(tmp_path: Path) -> None:
    runner = FakeRunner()
    plan = build_install_plan(exec_path=EXEC, unit_dir=tmp_path, runner=runner)
    outcome = apply_install(plan, runner=runner)

    written = sorted(p.name for p in tmp_path.iterdir())
    assert written == sorted([COLLECT_UNIT, TARGET_UNIT, WEB_UNIT])
    assert len(outcome["written"]) == 3

    ran = [" ".join(c["command"]) for c in outcome["ran"]]  # type: ignore[index]
    assert ran[0] == "systemctl --user daemon-reload", "daemon-reload must precede enable"
    assert any("enable-linger" in c for c in ran)
    assert any(c.startswith("systemctl --user enable --now") for c in ran)


def test_apply_install_is_idempotent(tmp_path: Path) -> None:
    """A reinstall after an upgrade must be safe: rewrite, re-enable, no error."""
    runner = FakeRunner()
    plan = build_install_plan(exec_path=EXEC, unit_dir=tmp_path, runner=runner)
    apply_install(plan, runner=runner)
    apply_install(plan, runner=runner)

    assert (tmp_path / COLLECT_UNIT).read_text(encoding="utf-8").count("[Service]") == 1


def test_apply_install_raises_with_a_remediation_when_a_command_fails(tmp_path: Path) -> None:
    runner = FakeRunner(fail_on="enable-linger")
    plan = build_install_plan(exec_path=EXEC, unit_dir=tmp_path, runner=FakeRunner())

    with pytest.raises(ServiceError) as excinfo:
        apply_install(plan, runner=runner)
    assert "enable-linger" in excinfo.value.message
    assert "logged in" in excinfo.value.remediation


# --- uninstall -------------------------------------------------------------


def test_uninstall_plan_only_lists_units_that_exist(tmp_path: Path) -> None:
    (tmp_path / COLLECT_UNIT).write_text("x", encoding="utf-8")
    plan = build_uninstall_plan(unit_dir=tmp_path)

    assert plan["remove"] == [COLLECT_UNIT]


def test_uninstall_plan_writes_nothing_and_removes_nothing(tmp_path: Path) -> None:
    (tmp_path / COLLECT_UNIT).write_text("x", encoding="utf-8")
    build_uninstall_plan(unit_dir=tmp_path)

    assert (tmp_path / COLLECT_UNIT).is_file(), "the plan builder must not delete anything"


def test_apply_uninstall_removes_the_units(tmp_path: Path) -> None:
    runner = FakeRunner()
    for name in (COLLECT_UNIT, WEB_UNIT, TARGET_UNIT):
        (tmp_path / name).write_text("x", encoding="utf-8")

    outcome = apply_uninstall(build_uninstall_plan(unit_dir=tmp_path), runner=runner)

    assert list(tmp_path.iterdir()) == []
    assert len(outcome["removed"]) == 3
    ran = [" ".join(c["command"]) for c in outcome["ran"]]  # type: ignore[index]
    assert any("disable --now" in c for c in ran)


def test_uninstall_never_disables_lingering(tmp_path: Path) -> None:
    """The operator may have enabled lingering for something else entirely."""
    (tmp_path / COLLECT_UNIT).write_text("x", encoding="utf-8")
    plan = build_uninstall_plan(unit_dir=tmp_path)

    for command in plan["commands"]:  # type: ignore[union-attr]
        assert "linger" not in " ".join(command)


# --- status ----------------------------------------------------------------


def test_status_reports_not_installed_on_an_empty_unit_dir(tmp_path: Path) -> None:
    state = status(unit_dir=tmp_path, runner=FakeRunner(), user="someone")

    assert state["installed"] is False
    assert all(u["installed"] is False for u in state["units"])  # type: ignore[union-attr]


def test_status_reports_enabled_active_and_linger(tmp_path: Path) -> None:
    for name in (COLLECT_UNIT, WEB_UNIT, TARGET_UNIT):
        (tmp_path / name).write_text("x", encoding="utf-8")

    state = status(unit_dir=tmp_path, runner=FakeRunner(linger=True), user="someone")

    assert state["installed"] is True
    assert state["linger"] is True
    units = state["units"]  # type: ignore[union-attr]
    collect = next(u for u in units if u["unit"] == COLLECT_UNIT)
    assert collect["enabled"] == "enabled"
    assert collect["active"] == "active"


# --- exec path resolution --------------------------------------------------


def test_resolve_exec_path_honours_an_explicit_override(tmp_path: Path) -> None:
    script = tmp_path / "sensibo"
    script.write_text("#!/bin/sh\n", encoding="utf-8")

    assert resolve_exec_path(str(script)) == str(script.resolve())


def test_resolve_exec_path_rejects_a_missing_override(tmp_path: Path) -> None:
    with pytest.raises(ServiceError) as excinfo:
        resolve_exec_path(str(tmp_path / "nope"))
    assert "does not exist" in excinfo.value.message
