"""Resolve where the local sqlite store lives on disk.

Precedence, highest first:

1. An explicit path passed by the caller (e.g. a test's ``tmp_path``).
2. The ``SENSIBO_DB`` environment variable.
3. The default, ``~/.sensibo/sensibo.db``.

Nothing here touches the filesystem — resolving a path never creates it.
Directory creation is :func:`sensibo.store.db.ensure_parent_dir`'s job, called
from :class:`sensibo.store.Store` at connect time.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Environment variable that overrides the default db path.
ENV_VAR = "SENSIBO_DB"

#: Default location, relative to the operator's home directory.
_DEFAULT_RELATIVE = Path(".sensibo") / "sensibo.db"


def default_db_path() -> Path:
    """Return the db path implied by ``SENSIBO_DB``, or the ``~/.sensibo`` default.

    Does not consult any caller-supplied override — see
    :func:`resolve_db_path` for the full precedence chain used by
    :class:`~sensibo.store.Store`.
    """
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / _DEFAULT_RELATIVE


def resolve_db_path(db_path: str | os.PathLike[str] | None) -> Path:
    """Resolve the effective db path for a :class:`~sensibo.store.Store`.

    ``db_path`` (a caller-supplied parameter) wins if given; otherwise falls
    back to :func:`default_db_path` (``SENSIBO_DB`` env var, then
    ``~/.sensibo/sensibo.db``).
    """
    if db_path is not None:
        return Path(db_path)
    return default_db_path()
