"""Install, remove, and inspect the systemd user units — the side-effecting half.

Everything that writes a file or shells out to ``systemctl`` / ``loginctl``
lives here, behind two seams so no test ever touches a real systemd:

* ``runner`` — every subprocess call goes through a :class:`Runner` callable.
  Tests inject a fake that records argv and returns canned results.
* ``unit_dir`` — every file write is rooted here. Tests point it at
  ``tmp_path``.

The dry-run/apply contract is structural, not a flag check bolted on top:
:func:`build_install_plan` is pure (it *describes* the writes and commands and
performs none of them), and :func:`apply_install` is the only function that
executes one. A caller that forgets ``--apply`` cannot accidentally mutate the
system, because the code path that mutates does not run.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess  # nosec B404 - argv lists only, never shell=True; see default_runner
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ._errors import ServiceError
from ._units import (
    COLLECT_UNIT,
    DEFAULT_UNIT_DIR,
    TARGET_UNIT,
    WEB_UNIT,
    UnitFile,
    render_collect_unit,
    render_target,
    render_web_unit,
)

#: The console command this project installs (`sensibo`), not the dist name.
_CONSOLE_SCRIPT = "sensibo"

#: Every unit this manager owns. Order matters for uninstall (target last).
ALL_UNITS = (COLLECT_UNIT, WEB_UNIT, TARGET_UNIT)


@dataclass(frozen=True)
class RunResult:
    """One subprocess invocation's outcome. Never raises on a non-zero exit."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def default_runner(argv: list[str]) -> RunResult:
    """Run ``argv`` with no shell. Non-zero exits are data, not exceptions.

    ``systemctl is-active`` exits non-zero for an inactive unit — that is an
    answer, not a failure — so this never uses ``check=True``.
    """
    proc = subprocess.run(  # nosec B603 - fixed argv list, shell=False, no user string splitting
        argv,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return RunResult(
        argv=tuple(argv),
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


#: A callable with :func:`default_runner`'s signature. Injected in tests.
Runner = object


def current_user() -> str:
    """The login name lingering is enabled for."""
    for var in ("LOGNAME", "USER", "USERNAME"):
        value = os.environ.get(var)
        if value:
            return value
    return str(Path.home().name)


def systemd_available() -> bool:
    """Is this a Linux box with ``systemctl`` and ``loginctl`` on PATH?"""
    if platform.system() != "Linux":
        return False
    return bool(shutil.which("systemctl")) and bool(shutil.which("loginctl"))


def require_systemd() -> None:
    """Raise a :class:`ServiceError` unless this host can actually run the units."""
    if platform.system() != "Linux":
        raise ServiceError(
            message=f"systemd user services need Linux; this host is {platform.system()}",
            remediation=(
                "run the collector under this platform's own supervisor "
                "(launchd on macOS, a Task Scheduler entry on Windows), or host it "
                "on a Linux box that stays awake"
            ),
        )
    missing = [tool for tool in ("systemctl", "loginctl") if not shutil.which(tool)]
    if missing:
        raise ServiceError(
            message=f"not found on PATH: {', '.join(missing)}",
            remediation=(
                "install systemd, or run 'sensibo collect --daemon' under your own supervisor"
            ),
        )


def resolve_exec_path(override: str | None = None) -> str:
    """Find the absolute ``sensibo`` executable to bake into ``ExecStart=``.

    systemd requires an absolute path and inherits none of the shell's ``PATH``
    or virtualenv activation, so "it works when I type ``sensibo``" is not
    enough — the unit needs the real file. Resolution order:

    1. ``override`` (``--exec-path``), if given — must exist.
    2. ``sensibo`` on the current ``PATH`` (the venv is active, or it was
       installed with ``uv tool`` / ``pipx``).
    3. A ``sensibo`` sibling of the running interpreter (``uv run sensibo
       service install`` — ``sys.executable`` is the venv's python, and the
       console script sits next to it).

    Falling back to ``python -m sensibo`` is deliberately *not* done: it would
    silently produce a working unit pinned to an interpreter the operator may
    not expect. Better to fail with a remediation.
    """
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise ServiceError(
                message=f"--exec-path does not exist: {candidate}",
                remediation="pass the absolute path of the installed 'sensibo' console script",
            )
        return str(candidate.resolve())

    found = shutil.which(_CONSOLE_SCRIPT)
    if found:
        return str(Path(found).resolve())

    sibling = Path(sys.executable).parent / _CONSOLE_SCRIPT
    if sibling.is_file():
        return str(sibling.resolve())

    raise ServiceError(
        message=f"could not find the '{_CONSOLE_SCRIPT}' console script to run from the unit",
        remediation=(
            "install it on PATH (e.g. 'uv tool install sensibo-cli' or 'pip install -e .'), "
            "or pass --exec-path /abs/path/to/sensibo"
        ),
    )


def linger_enabled(user: str, *, runner=default_runner) -> bool:
    """Is systemd lingering already on for ``user``?

    Lingering is what makes a *user* manager start at boot instead of at
    login — without it these units only run while someone is logged in, which
    is not "always-on" at all.
    """
    result = runner(["loginctl", "show-user", user, "--property=Linger"])
    if not result.ok:
        return False
    return "Linger=yes" in result.stdout


@dataclass(frozen=True)
class InstallPlan:
    """Exactly what ``service install --apply`` would write and run. Executes nothing."""

    exec_path: str
    unit_dir: Path
    units: tuple[UnitFile, ...]
    commands: tuple[tuple[str, ...], ...]
    linger_user: str
    linger_already_enabled: bool
    warnings: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        return {
            "exec_path": self.exec_path,
            "unit_dir": str(self.unit_dir),
            "units": [
                {"name": u.name, "path": str(self.unit_dir / u.name), "content": u.content}
                for u in self.units
            ],
            "commands": [list(c) for c in self.commands],
            "linger_user": self.linger_user,
            "linger_already_enabled": self.linger_already_enabled,
            "warnings": list(self.warnings),
        }


def build_install_plan(
    *,
    exec_path: str,
    unit_dir: Path | None = None,
    collect: bool = True,
    web: bool = True,
    interval: float = 60.0,
    bind: str = "0.0.0.0:8323",
    db: str | None = None,
    token_file: str | None = None,
    linger_user: str | None = None,
    runner=default_runner,
) -> InstallPlan:
    """Describe the install. Pure — writes nothing, runs nothing that mutates.

    ``linger_enabled`` is the one thing it *reads* from the system (a
    ``loginctl show-user`` query), so the plan can honestly say whether
    ``enable-linger`` is still needed or already done.
    """
    if not collect and not web:
        raise ServiceError(
            message="nothing to install: both --no-collect and --no-web were passed",
            remediation="drop one of them — the target needs at least one service",
        )

    target_dir = Path(unit_dir) if unit_dir is not None else DEFAULT_UNIT_DIR
    user = linger_user or current_user()

    units: list[UnitFile] = []
    enable: list[str] = []
    if collect:
        units.append(render_collect_unit(exec_path, interval=interval, db=db))
        enable.append(COLLECT_UNIT)
    if web:
        units.append(render_web_unit(exec_path, bind=bind, db=db, token_file=token_file))
        enable.append(WEB_UNIT)
    units.append(render_target())

    already_lingering = linger_enabled(user, runner=runner)

    commands: list[tuple[str, ...]] = [("systemctl", "--user", "daemon-reload")]
    if not already_lingering:
        commands.append(("loginctl", "enable-linger", user))
    commands.append(
        ("systemctl", "--user", "enable", "--now", TARGET_UNIT, *enable),
    )

    warnings: list[str] = []
    if not systemd_available():
        warnings.append(
            "systemd is not available on this host — this plan describes what would "
            "be done, but --apply would fail here"
        )

    return InstallPlan(
        exec_path=exec_path,
        unit_dir=target_dir,
        units=tuple(units),
        commands=tuple(commands),
        linger_user=user,
        linger_already_enabled=already_lingering,
        warnings=tuple(warnings),
    )


def _write_unit(unit_dir: Path, unit: UnitFile) -> Path:
    unit_dir.mkdir(parents=True, exist_ok=True)
    path = unit_dir / unit.name
    path.write_text(unit.content, encoding="utf-8")
    return path


def apply_install(plan: InstallPlan, *, runner=default_runner) -> dict[str, object]:
    """Execute ``plan``: write the units, then run its commands in order.

    Idempotent — rewriting a unit and re-running ``enable --now`` on an
    already-enabled unit is a no-op to systemd, so a reinstall after an
    upgrade is safe.
    """
    written = [str(_write_unit(plan.unit_dir, unit)) for unit in plan.units]

    ran: list[dict[str, object]] = []
    for argv in plan.commands:
        result = runner(list(argv))
        ran.append(
            {
                "command": list(argv),
                "returncode": result.returncode,
                "stderr": result.stderr.strip(),
            }
        )
        if not result.ok:
            raise ServiceError(
                message=f"command failed ({result.returncode}): {' '.join(argv)}\n"
                f"{result.stderr.strip()}",
                remediation=_command_remediation(argv),
            )

    return {"written": written, "ran": ran}


def _command_remediation(argv: tuple[str, ...]) -> str:
    """A remediation tuned to which command blew up — generic hints help nobody."""
    joined = " ".join(argv)
    if "enable-linger" in joined:
        return (
            "lingering could not be enabled (some systems restrict it); without it the "
            "units only run while you are logged in — ask an admin, or check "
            "'loginctl show-user'"
        )
    if "--user" in joined:
        return (
            "the systemd *user* manager is not reachable — check 'systemctl --user status'. "
            "Over SSH into a fresh session this often needs XDG_RUNTIME_DIR set, or a "
            "prior 'loginctl enable-linger'"
        )
    return "check the command output above, then re-run 'sensibo service install --apply'"


def build_uninstall_plan(*, unit_dir: Path | None = None) -> dict[str, object]:
    """Describe the removal: disable + stop, then delete whichever units exist."""
    target_dir = Path(unit_dir) if unit_dir is not None else DEFAULT_UNIT_DIR
    present = [name for name in ALL_UNITS if (target_dir / name).is_file()]

    commands: list[tuple[str, ...]] = []
    if present:
        commands.append(("systemctl", "--user", "disable", "--now", *present))
    commands.append(("systemctl", "--user", "daemon-reload"))

    return {
        "unit_dir": target_dir,
        "remove": present,
        "commands": commands,
        # Lingering is deliberately left ON: the operator may well have enabled it
        # for something else, and turning it off would silently break that.
        "linger": "left enabled (uninstall never disables lingering)",
    }


def apply_uninstall(plan: dict[str, object], *, runner=default_runner) -> dict[str, object]:
    """Execute an uninstall plan. Missing units are not an error — removal is idempotent."""
    unit_dir = Path(str(plan["unit_dir"]))
    removed: list[str] = []

    ran: list[dict[str, object]] = []
    for argv in plan["commands"]:  # type: ignore[union-attr]
        result = runner(list(argv))
        ran.append({"command": list(argv), "returncode": result.returncode})

    for name in plan["remove"]:  # type: ignore[union-attr]
        path = unit_dir / str(name)
        if path.is_file():
            path.unlink()
            removed.append(str(path))

    return {"removed": removed, "ran": ran}


def unit_status(name: str, *, unit_dir: Path, runner=default_runner) -> dict[str, object]:
    """One unit's state: installed on disk, enabled at boot, active right now."""
    path = unit_dir / name
    if not path.is_file():
        return {"unit": name, "installed": False, "enabled": None, "active": None}

    is_enabled = runner(["systemctl", "--user", "is-enabled", name])
    is_active = runner(["systemctl", "--user", "is-active", name])
    return {
        "unit": name,
        "installed": True,
        # is-enabled/is-active exit non-zero for disabled/inactive units and still
        # print the state word — the stdout is the answer, the exit code is noise.
        "enabled": is_enabled.stdout.strip() or "unknown",
        "active": is_active.stdout.strip() or "unknown",
    }


def status(
    *,
    unit_dir: Path | None = None,
    runner=default_runner,
    user: str | None = None,
) -> dict[str, object]:
    """The whole always-on picture: per-unit state plus the lingering flag."""
    target_dir = Path(unit_dir) if unit_dir is not None else DEFAULT_UNIT_DIR
    who = user or current_user()

    units = [unit_status(name, unit_dir=target_dir, runner=runner) for name in ALL_UNITS]
    return {
        "unit_dir": str(target_dir),
        "user": who,
        "linger": linger_enabled(who, runner=runner),
        "units": units,
        "installed": any(u["installed"] for u in units),
    }
