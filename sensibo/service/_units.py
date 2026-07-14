"""Render the systemd **user** units that keep collection and the dashboard alive.

Pure rendering: every function here returns text and touches nothing. The
side-effecting half — writing files, running ``systemctl`` — lives in
:mod:`sensibo.service.manager`, so a test can assert on exact unit content
without a systemd anywhere near it.

Why *user* units, not system units
----------------------------------
No root, no ``/etc``, no ``sudo`` — they live in ``~/.config/systemd/user``
and run as the operator. The price is that a user manager normally starts at
login and dies at logout, which would make "always-on" a lie. ``loginctl
enable-linger $USER`` is what buys it back: with lingering on, systemd starts
the user manager **at boot**, no login required, and stops it only at
shutdown. That single command is the whole auto-start-on-reboot story;
:mod:`sensibo.service.manager` puts it in the install plan.

Why ``Restart=always`` is load-bearing, not decoration
------------------------------------------------------
``sensibo collect --daemon`` is not internally resilient: an
:class:`~sensibo.api.ApiError` (network down at boot, cloud 5xx, an exhausted
429 retry) propagates out of its loop and exits the process with code 2 — see
``sensibo/cli/_commands/collect.py``. systemd is the supervisor that makes the
collector survive that, and it is the *only* thing that does. Without
``Restart=always`` the first transient cloud blip ends collection silently,
and the ~7-day cloud history window means the resulting gap is unrecoverable.

The unit set
------------
``sensibo.target`` groups the services; each service declares
``WantedBy=sensibo.target`` (so ``systemctl --user enable`` links it into the
target) and ``PartOf=sensibo.target`` (so stopping the target stops it). The
target itself is ``WantedBy=default.target`` — that plus lingering is what
starts everything at boot.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sensibo.collect import MIN_INTERVAL

#: Where systemd looks for a user's own units.
DEFAULT_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

TARGET_UNIT = "sensibo.target"
COLLECT_UNIT = "sensibo-collect.service"
WEB_UNIT = "sensibo-web.service"

#: The dashboard makes no cloud calls on its read path, so a tight restart is free.
_WEB_RESTART_SEC = 5

#: Exponential restart backoff for the collector, for systemd >= 254 only
#: (``RestartSteps`` / ``RestartMaxDelaySec`` did not exist before that).
#: Ceiling deliberately well above a poll interval: during a *long* cloud outage
#: there is nothing to collect anyway, so retrying every 15 minutes rather than
#: every minute costs no data and stops us pounding an API that is already
#: refusing us. `collect` backfills what the cloud still holds on the next
#: successful cycle, so a late recovery self-heals.
_RESTART_MAX_DELAY_SEC = 900
_RESTART_STEPS = 5

#: First systemd release with RestartSteps/RestartMaxDelaySec.
SYSTEMD_BACKOFF_MIN_VERSION = 254

_DOC_URL = "https://github.com/agentculture/sensibo-cli/blob/main/docs/deployment.md"


def _quote(arg: str) -> str:
    """Quote one ``ExecStart`` argument if systemd's whitespace split would break it.

    systemd splits ``ExecStart`` on whitespace and honours double quotes
    (``systemd.syntax(7)``). A venv under ``/home/some user/`` or a ``--db``
    path with a space would otherwise silently become two arguments.
    """
    if not arg:
        return '""'
    if any(ch.isspace() for ch in arg) or '"' in arg:
        escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return arg


def exec_line(parts: list[str]) -> str:
    """Join an argv list into an ``ExecStart=`` value, quoting where needed."""
    return " ".join(_quote(p) for p in parts)


@dataclass(frozen=True)
class UnitFile:
    """One rendered unit: the filename systemd expects, and its full content."""

    name: str
    content: str


def render_target() -> UnitFile:
    """The grouping target. ``WantedBy=default.target`` + lingering = start at boot."""
    content = f"""\
# Managed by `sensibo service install`. Edits are overwritten on reinstall.
[Unit]
Description=sensibo-cli: always-on sensor collection and LAN dashboard
Documentation={_DOC_URL}

[Install]
WantedBy=default.target
"""
    return UnitFile(TARGET_UNIT, content)


def collect_restart_sec(interval: float) -> int:
    """How long to wait before restarting a collector that just died.

    **Never shorter than the poll interval, and never below ``MIN_INTERVAL``.**
    This is the whole point of the function, and getting it wrong is a live
    rate-limit hazard: ``collect --daemon`` exits on any ``ApiError``, so a
    restart delay *below* the poll cadence would make a **failing** collector
    hit Sensibo's API **more often than a healthy one** — hammering an API that
    is, by hypothesis, already erroring or rate-limiting us. ``MIN_INTERVAL``
    (60s) is the floor ``sensibo collect`` itself enforces as Sensibo's safe
    polling rate; a restart loop must respect the same floor.
    """
    return int(max(MIN_INTERVAL, interval))


def render_collect_unit(
    exec_path: str,
    *,
    interval: float,
    db: str | None = None,
    systemd_version: int | None = None,
) -> UnitFile:
    """The collector: poll the fleet on a cadence, persist into the local store.

    No ``EnvironmentFile=`` for the API key, deliberately. The key resolves
    inside the client (``SENSIBO_API_KEY``, then ``~/.sensibo/.env`` — see
    :mod:`sensibo.api._auth`), so systemd never parses the dotenv, never holds
    the key in a unit file, and never logs it. A dotenv written for a shell
    (``export K=v``, quoted values) is not systemd's ``EnvironmentFile``
    format anyway; letting the client own the parse avoids that trap entirely.

    ``systemd_version``, when >= :data:`SYSTEMD_BACKOFF_MIN_VERSION`, adds
    exponential restart backoff. Omitted on older systemd rather than emitted
    and ignored — an unknown directive is only a journal warning, but a unit
    that lies about its own restart policy is worse than one that is plain.
    """
    argv = [exec_path, "collect", "--daemon", "--interval", f"{interval:g}"]
    if db:
        argv += ["--db", db]

    restart_sec = collect_restart_sec(interval)
    backoff = ""
    if systemd_version is not None and systemd_version >= SYSTEMD_BACKOFF_MIN_VERSION:
        backoff = (
            f"# Exponential backoff (systemd >= {SYSTEMD_BACKOFF_MIN_VERSION}): a persistent\n"
            f"# failure (cloud down, 429s) backs off toward {_RESTART_MAX_DELAY_SEC}s instead of\n"
            f"# retrying at {restart_sec}s forever. It never gives up — giving up loses data.\n"
            f"RestartSteps={_RESTART_STEPS}\n"
            f"RestartMaxDelaySec={_RESTART_MAX_DELAY_SEC}\n"
        )

    content = f"""\
# Managed by `sensibo service install`. Edits are overwritten on reinstall.
[Unit]
Description=sensibo-cli: poll the Sensibo fleet into the local store
Documentation={_DOC_URL}
PartOf={TARGET_UNIT}

[Service]
Type=simple
ExecStart={exec_line(argv)}
# The daemon exits (code 2) on an ApiError — a cloud blip, or the network not
# being up yet at boot. systemd is what makes collection survive that; the
# ~7-day cloud history window means a gap it does not recover is lost forever.
#
# Restart=always, never a start limit: a start limit would stop retrying after a
# long outage, and a collector that has given up is exactly the failure this
# whole unit exists to prevent. RestartSec is floored at the collector's own
# MIN_INTERVAL ({int(MIN_INTERVAL)}s) so a FAILING collector can never poll the API
# faster than a healthy one.
Restart=always
RestartSec={restart_sec}
{backoff}# The API key is resolved by the client (SENSIBO_API_KEY, else ~/.sensibo/.env).
# It is deliberately NOT named here: a unit file is world-readable.

[Install]
WantedBy={TARGET_UNIT}
"""
    return UnitFile(COLLECT_UNIT, content)


def render_web_unit(
    exec_path: str,
    *,
    bind: str,
    db: str | None = None,
    token_file: str | None = None,
) -> UnitFile:
    """The dashboard: always up, so an operator can always come and look."""
    argv = [exec_path, "web", "--bind", bind]
    if db:
        argv += ["--db", db]
    if token_file:
        argv += ["--token-file", token_file]
    content = f"""\
# Managed by `sensibo service install`. Edits are overwritten on reinstall.
[Unit]
Description=sensibo-cli: LAN dashboard (open reads, token-gated writes)
Documentation={_DOC_URL}
PartOf={TARGET_UNIT}

[Service]
Type=simple
ExecStart={exec_line(argv)}
Restart=always
RestartSec={_WEB_RESTART_SEC}
# Reads serve from the local store, so the dashboard keeps working with the
# Sensibo cloud unreachable. Writes stay token-gated (~/.sensibo/web-token).

[Install]
WantedBy={TARGET_UNIT}
"""
    return UnitFile(WEB_UNIT, content)
