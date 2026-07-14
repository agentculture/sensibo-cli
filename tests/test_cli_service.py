"""CLI tests for ``sensibo service`` — the always-on units.

The engine is exercised in ``tests/test_service_units.py``; here we check the
CLI plumbing, and above all **the write-verb contract this repo makes
mandatory**: a write verb without ``--apply`` must print exactly what it would
do and change nothing. Here a write installs a background daemon that drives an
air conditioner's data path on someone's home machine, so "it defaults to
dry-run" is a safety property, not a nicety.

No test shells out to a real ``systemctl``: the ``_runner`` seam in the CLI
module is monkeypatched to a fake, and ``--unit-dir`` points every write at
``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sensibo.cli._commands.service as service_module
import sensibo.service.manager as manager_module
from sensibo.cli import main
from sensibo.explain import known_paths
from sensibo.service import COLLECT_UNIT, TARGET_UNIT, WEB_UNIT, RunResult


class FakeRunner:
    def __init__(self, *, linger: bool = False, version: int = 255) -> None:
        self.calls: list[list[str]] = []
        self._linger = linger
        self._version = version

    def __call__(self, argv: list[str]) -> RunResult:
        self.calls.append(list(argv))
        if "--version" in argv:
            return RunResult(tuple(argv), returncode=0, stdout=f"systemd {self._version} (x)\n")
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


@pytest.fixture()
def fake_systemd(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """Nothing here touches the real systemd, on any platform."""
    runner = FakeRunner()
    monkeypatch.setattr(service_module, "_runner", lambda: runner)
    monkeypatch.setattr(service_module, "require_systemd", lambda: None)
    monkeypatch.setattr(service_module, "resolve_exec_path", lambda _=None: "/opt/venv/bin/sensibo")
    return runner


# --- the dry-run contract ---------------------------------------------------


def test_install_without_apply_writes_nothing(
    tmp_path: Path, fake_systemd: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    """The load-bearing one: no --apply, no units on disk, no systemctl run."""
    rc = main(["service", "install", "--unit-dir", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert list(tmp_path.iterdir()) == [], "dry-run must not write a unit file"
    assert "applied: no (dry-run — pass --apply to commit)" in out
    assert "would write:" in out
    assert "would run:" in out
    # A dry-run may only *query*: the linger check and the systemd version probe.
    # Any other command is a mutation, and a break of the write-verb contract.
    for call in fake_systemd.calls:
        assert "show-user" in call or "--version" in call, f"dry-run ran a mutating command: {call}"


def test_install_dry_run_names_every_file_and_command(
    tmp_path: Path, fake_systemd: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    """'Exactly what it would do' means naming it, not summarising it."""
    main(["service", "install", "--unit-dir", str(tmp_path)])
    out = capsys.readouterr().out

    for unit in (COLLECT_UNIT, WEB_UNIT, TARGET_UNIT):
        assert str(tmp_path / unit) in out
    assert "systemctl --user daemon-reload" in out
    assert "loginctl enable-linger" in out
    assert "systemctl --user enable --now" in out


def test_install_dry_run_can_show_the_unit_files(
    tmp_path: Path, fake_systemd: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["service", "install", "--unit-dir", str(tmp_path), "--show-units"])
    out = capsys.readouterr().out

    assert "Restart=always" in out
    assert "ExecStart=/opt/venv/bin/sensibo collect --daemon" in out


def test_uninstall_without_apply_removes_nothing(
    tmp_path: Path, fake_systemd: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / COLLECT_UNIT).write_text("x", encoding="utf-8")

    rc = main(["service", "uninstall", "--unit-dir", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert (tmp_path / COLLECT_UNIT).is_file(), "dry-run must not delete a unit"
    assert "applied: no (dry-run — pass --apply to commit)" in out
    assert "would remove:" in out


# --- apply ------------------------------------------------------------------


def test_install_apply_writes_the_units_and_enables_them(
    tmp_path: Path, fake_systemd: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["service", "install", "--unit-dir", str(tmp_path), "--apply"])
    out = capsys.readouterr().out

    assert rc == 0
    assert (tmp_path / COLLECT_UNIT).is_file()
    assert (tmp_path / WEB_UNIT).is_file()
    assert (tmp_path / TARGET_UNIT).is_file()
    assert "applied: yes" in out

    ran = [" ".join(c) for c in fake_systemd.calls]
    assert any("daemon-reload" in c for c in ran)
    assert any("enable-linger" in c for c in ran)
    assert any("enable --now" in c for c in ran)


def test_install_apply_bakes_the_flags_into_the_units(
    tmp_path: Path, fake_systemd: FakeRunner
) -> None:
    main(
        [
            "service",
            "install",
            "--unit-dir",
            str(tmp_path),
            "--interval",
            "120",
            "--bind",
            "127.0.0.1:9999",
            "--db",
            "/srv/s.db",
            "--apply",
        ]
    )

    collect = (tmp_path / COLLECT_UNIT).read_text(encoding="utf-8")
    web = (tmp_path / WEB_UNIT).read_text(encoding="utf-8")
    assert "--interval 120" in collect
    assert "--db /srv/s.db" in collect
    assert "--bind 127.0.0.1:9999" in web


def test_install_no_web_installs_only_the_collector(
    tmp_path: Path, fake_systemd: FakeRunner
) -> None:
    main(["service", "install", "--unit-dir", str(tmp_path), "--no-web", "--apply"])

    assert (tmp_path / COLLECT_UNIT).is_file()
    assert not (tmp_path / WEB_UNIT).exists()


def test_uninstall_apply_deletes_the_units(tmp_path: Path, fake_systemd: FakeRunner) -> None:
    for name in (COLLECT_UNIT, WEB_UNIT, TARGET_UNIT):
        (tmp_path / name).write_text("x", encoding="utf-8")

    rc = main(["service", "uninstall", "--unit-dir", str(tmp_path), "--apply"])

    assert rc == 0
    assert list(tmp_path.iterdir()) == []


# --- status -----------------------------------------------------------------


def test_status_reports_not_installed(
    tmp_path: Path, fake_systemd: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["service", "status", "--unit-dir", str(tmp_path), "--db", str(tmp_path / "no.db")])
    out = capsys.readouterr().out

    assert rc == 0
    assert "units:    not installed" in out
    # The store section prints even when the units do not exist — "you have data
    # and nothing is keeping it fresh" is the state this operator is actually in.
    assert "store:" in out


def test_status_never_creates_the_store_it_probes(
    tmp_path: Path, fake_systemd: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    """A status probe that materialises an empty db would report a lie by side effect."""
    db = tmp_path / "absent.db"
    main(["service", "status", "--unit-dir", str(tmp_path), "--db", str(db)])
    out = capsys.readouterr().out

    assert not db.exists(), "status must not create the store"
    assert "no store yet" in out


def test_status_json_carries_units_linger_and_store(
    tmp_path: Path, fake_systemd: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in (COLLECT_UNIT, WEB_UNIT, TARGET_UNIT):
        (tmp_path / name).write_text("x", encoding="utf-8")

    main(
        [
            "service",
            "status",
            "--unit-dir",
            str(tmp_path),
            "--db",
            str(tmp_path / "no.db"),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["installed"] is True
    assert payload["linger"] is False
    assert {u["unit"] for u in payload["units"]} == {COLLECT_UNIT, WEB_UNIT, TARGET_UNIT}
    assert payload["store"]["exists"] is False
    assert "systemd-supervised" in payload["execution"]


# --- JSON install payload ---------------------------------------------------


def test_install_json_is_a_full_inspectable_plan(
    tmp_path: Path, fake_systemd: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    """An agent must be able to read the plan before committing to it."""
    main(["service", "install", "--unit-dir", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["apply"] is False
    assert payload["result"] is None
    assert len(payload["units"]) == 3
    assert all(u["content"] for u in payload["units"]), "the plan must carry the unit bodies"
    assert payload["commands"][0] == ["systemctl", "--user", "daemon-reload"]
    assert list(tmp_path.iterdir()) == []


# --- safety: the rules daemon is deliberately not installed -----------------


def test_the_rule_daemon_is_never_installed(tmp_path: Path, fake_systemd: FakeRunner) -> None:
    """`rule run --daemon` drives a compressor unattended — arming it stays explicit."""
    main(["service", "install", "--unit-dir", str(tmp_path), "--apply"])

    for unit in tmp_path.iterdir():
        assert "rule" not in unit.read_text(encoding="utf-8")


def test_install_says_out_loud_that_the_rule_daemon_is_excluded(
    tmp_path: Path, fake_systemd: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["service", "install", "--unit-dir", str(tmp_path)])
    out = capsys.readouterr().out

    assert "rule run --daemon" in out
    assert "NOT installed" in out


# --- errors route through the CLI contract ----------------------------------


def test_install_apply_on_a_non_systemd_host_is_an_environment_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit 2, a `hint:` line, and no traceback — the error contract, unbroken."""
    monkeypatch.setattr(service_module, "_runner", lambda: FakeRunner())
    monkeypatch.setattr(manager_module.platform, "system", lambda: "Darwin")

    rc = main(["service", "install", "--unit-dir", str(tmp_path), "--apply"])
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.err.startswith("error: ")
    assert "hint: " in captured.err
    assert "Traceback" not in captured.err
    assert list(tmp_path.iterdir()) == []


# --- agent-first surface ----------------------------------------------------


def test_service_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["service", "--help"])
    assert exc.value.code == 0
    assert "service" in capsys.readouterr().out.lower()


def test_bare_service_prints_the_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["service"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "sensibo service" in out


def test_every_service_verb_has_an_explain_entry() -> None:
    """The rubric gate: a command with no `explain` entry is a command agents can't learn."""
    paths = set(known_paths())
    for verb in ("overview", "install", "uninstall", "status"):
        assert ("service", verb) in paths, f"missing explain entry for 'service {verb}'"
