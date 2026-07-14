"""``sensibo.service`` — keep collection and the dashboard alive across reboots.

The three long-running loops this project ships (``collect --daemon``, ``web``,
``rule run --daemon``) are foreground processes: they die with the terminal,
the logout, and the reboot. That is fine for a demo and fatal for the product's
central claim — Sensibo's cloud only serves roughly the last **7 days** of
history, so a collection gap while the host is asleep is **permanently lost
data**, not a delayed sync. Retention needs an always-on collector.

This package is that deployment story, recorded as an open question in the
product spec ("Always-on host for the collector and rules daemon — which
machine, systemd unit, restart policy") and closed here.

What it installs (systemd **user** units — no root, no ``/etc``, no ``sudo``):

* ``sensibo-collect.service`` — ``collect --daemon``, ``Restart=always``
* ``sensibo-web.service`` — ``web``, ``Restart=always``
* ``sensibo.target`` — groups both; ``WantedBy=default.target``

plus ``loginctl enable-linger``, which is what makes a *user* manager start at
**boot** rather than at login. Without lingering the units are not always-on,
they are merely "on while you happen to be logged in".

``rule run --daemon`` is deliberately **not** installed: it drives a real
compressor unattended, so arming it is an explicit operator decision, not a
side effect of installing collection. See the tracking issue in
``docs/deployment.md``.

Layering: this package depends on nothing in :mod:`sensibo.cli`. Failures raise
:class:`ServiceError`, which the CLI maps onto its own error contract.
"""

from __future__ import annotations

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
from .manager import (
    ALL_UNITS,
    InstallPlan,
    RunResult,
    apply_install,
    apply_uninstall,
    build_install_plan,
    build_uninstall_plan,
    current_user,
    default_runner,
    linger_enabled,
    require_systemd,
    resolve_exec_path,
    status,
    systemd_available,
)

__all__ = [
    "ServiceError",
    "InstallPlan",
    "RunResult",
    "UnitFile",
    "ALL_UNITS",
    "COLLECT_UNIT",
    "WEB_UNIT",
    "TARGET_UNIT",
    "DEFAULT_UNIT_DIR",
    "build_install_plan",
    "apply_install",
    "build_uninstall_plan",
    "apply_uninstall",
    "status",
    "default_runner",
    "resolve_exec_path",
    "require_systemd",
    "systemd_available",
    "linger_enabled",
    "current_user",
    "render_collect_unit",
    "render_web_unit",
    "render_target",
]
