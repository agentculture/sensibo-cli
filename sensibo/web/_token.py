"""The write-auth token: generate once, persist, never log the value (task t12).

Recorded operator decision (``docs/specs/...`` -> "Resolved operator
decisions" -> "Web dashboard access"): **reads are open on the LAN, writes are
token-gated.** This module owns exactly that token's lifecycle:

* :func:`ensure_token` generates a random token on first use
  (``secrets.token_hex``, cryptographically strong) and persists it to disk
  mode ``0600``; a later call reuses whatever is already on disk, so the token
  survives a restart of ``sensibo web``.
* :func:`check_token` is the only comparison callers should use — it is a
  constant-time (:func:`hmac.compare_digest`) check, so a timing side-channel
  can't leak the token a byte at a time.

**This module never prints the token.** The default path
(``~/.sensibo/web-token``) is safe to log; the value is not — printing it is
the CLI layer's responsibility to avoid (``sensibo/cli/_commands/web.py``
logs the *path*, never the token returned here).
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

#: Default token file location, mirroring ``sensibo/store/_paths.py``'s
#: ``~/.sensibo``-rooted convention.
DEFAULT_TOKEN_FILE = Path.home() / ".sensibo" / "web-token"

#: Bytes of randomness in a generated token (-> 64 hex characters).
_TOKEN_NBYTES = 32


def default_token_path() -> Path:
    """Where the token lives absent an explicit override."""
    return DEFAULT_TOKEN_FILE


def _ensure_parent_dir(path: Path) -> None:
    """Create ``path``'s parent directory with restrictive (0700) permissions.

    Mirrors :func:`sensibo.store.store._ensure_parent_dir` — this module is
    intentionally independent of :mod:`sensibo.store` (no cross-package
    coupling for a two-line helper).
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    os.chmod(parent, 0o700)


def _write_token(path: Path, token: str) -> None:
    """Write ``token`` to ``path`` with mode 0600, atomically enough for a CLI tool.

    ``os.open`` with an explicit mode (rather than ``Path.write_text`` then
    ``chmod``) avoids a window where the file briefly exists with the
    process's default (umask-dependent, possibly wider) permissions.
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
    finally:
        os.chmod(path, 0o600)  # belt-and-suspenders against an inherited umask


def ensure_token(path: str | os.PathLike[str] | None = None) -> tuple[str, Path]:
    """Load the persisted token at ``path``, generating one on first use.

    Returns ``(token, path)`` — ``path`` resolved to a :class:`Path` (the
    caller-supplied override if given, else :func:`default_token_path`).
    A pre-existing, non-empty file wins over generating a new token, so
    restarting ``sensibo web`` does not invalidate a token an operator has
    already copied into a bookmark or a script.
    """
    target = Path(path) if path is not None else default_token_path()
    _ensure_parent_dir(target)

    if target.is_file():
        existing = target.read_text(encoding="utf-8").strip()
        if existing:
            return existing, target

    token = secrets.token_hex(_TOKEN_NBYTES)
    _write_token(target, token)
    return token, target


def check_token(candidate: str | None, expected: str) -> bool:
    """Constant-time comparison of a caller-supplied token against ``expected``.

    ``None``/empty candidates always fail (never reach
    :func:`hmac.compare_digest` with a falsy left-hand side, which would leak
    a length-zero-vs-nonzero shortcut path the same way plain ``==`` would).
    """
    if not candidate:
        return False
    return hmac.compare_digest(candidate, expected)
