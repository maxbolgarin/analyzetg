"""Best-effort file-permission tightening for sensitive on-disk artifacts.

`~/.unread/` is mode 0o700, but the SQLite DB, Telethon session file,
report Markdown, and `.env` files inside it are created by libraries
(aiosqlite, Telethon, our own ``write_text``) that respect the process
umask — typically 0o644, world-readable. On any multi-user host (shared
dev box, CI runner, family Mac) other local users can then read every
cached chat message, every analysis report, and the on-disk API keys.

Two helpers:

* :func:`tighten` — chmod after the fact. Fine for files that don't yet
  contain secrets at write time (reports, the seeded `.env` template)
  but leaves a sub-millisecond window during which the file lives at
  the umask's default mode. Existing callers stay on this path.
* :func:`secret_write_text` — opens with ``O_CREAT|O_WRONLY|O_TRUNC``
  and explicit mode 0o600 *before* any bytes are written, so a file
  whose first write contains a credential is never world-readable
  even briefly. Use this for any new writer that puts secrets in a
  file from byte zero (passphrase-derived keys, future encrypted
  blobs, the install pointer).

Failures are logged but never propagated: network mounts, ACL
filesystems (Windows on SMB, AFS), and read-only mounts can all reject
``chmod`` legitimately and we don't want a chmod miss to abort an
otherwise successful operation.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

from unread.util.logging import get_logger

log = get_logger(__name__)

# 0o600 — owner read/write, no group, no world. The default for every
# on-disk secret-bearing artifact under ``~/.unread/``.
SECRET_FILE_MODE = 0o600

# 0o700 — owner-only access for directories that hold sensitive
# artifacts (downloaded media, temp transcoding output).
PRIVATE_DIR_MODE = 0o700


def tighten(path: Path, mode: int = SECRET_FILE_MODE) -> bool:
    """Apply ``mode`` to ``path`` if possible; log on failure.

    Returns True on success, False if the chmod failed for any reason
    (file missing, network mount, Windows ACL, read-only fs). The
    caller may surface the warning to the user when appropriate
    (e.g. first-run setup); routine writers can ignore the return.
    """
    try:
        path.chmod(mode)
        return True
    except OSError as e:
        log.warning(
            "fsmode.chmod_failed",
            path=str(path),
            mode=oct(mode),
            err=str(e)[:200],
        )
        return False


def secret_write_text(
    path: Path,
    data: str,
    *,
    encoding: str = "utf-8",
    mode: int = SECRET_FILE_MODE,
) -> None:
    """Write ``data`` to ``path`` so the file is mode 0o600 from creation.

    Uses ``os.open(O_CREAT|O_WRONLY|O_TRUNC, mode)`` to avoid the
    umask race that ``Path.write_text`` + ``tighten`` leaves open.
    Calls ``tighten`` afterward as a belt-and-braces step in case the
    file already existed with a looser mode (``O_CREAT``'s mode bits
    are only applied on creation, not on an existing file). Parent
    directory must already exist — callers handle ``mkdir`` with the
    appropriate dir mode (typically 0o700).

    On platforms where the underlying ``open`` flags aren't supported
    (e.g. Windows ACL drives) we fall back to a regular write + chmod.
    Same failure semantics as :func:`tighten`: errors are logged, never
    raised — except for permission/space errors writing the *data*,
    which propagate so callers can surface a real failure.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    # O_NOFOLLOW protects against a symlink swap where another user
    # races us to point ``path`` at a file they want our secrets in.
    # Not all platforms have it (Windows); guard accordingly.
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | nofollow, mode)
    except OSError as e:
        # ELOOP: ``path`` exists and is a symlink. Refuse to follow —
        # surface a clear error rather than silently writing through.
        if e.errno == errno.ELOOP:
            log.warning(
                "fsmode.refused_symlink",
                path=str(path),
                hint="path is a symlink; remove it before writing",
            )
            raise
        # Fall back: open via the high-level API and chmod after.
        # Best-effort; this is the Windows / odd-fs path.
        path.write_text(data, encoding=encoding)
        tighten(path, mode)
        return
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(data)
    except Exception:
        # On write failure, leave the partial file behind for
        # debugging — but ensure perms are still tight. The caller
        # will see the exception and decide what to do.
        tighten(path, mode)
        raise
    # If ``path`` already existed, ``O_CREAT``'s mode bits were
    # ignored. A trailing chmod is cheap and idempotent.
    tighten(path, mode)


def ensure_private_dir(path: Path, mode: int = PRIVATE_DIR_MODE) -> Path:
    """``mkdir(parents=True, exist_ok=True)`` then chmod to ``mode``.

    Centralizes the "create a directory that holds Telegram media or
    transcode scratch space" pattern. Existing directories get their
    mode tightened too, so an upgrade from a pre-hardening install
    fixes up an old 0o755 media folder on the next analyze run.
    """
    path.mkdir(parents=True, exist_ok=True)
    tighten(path, mode)
    return path
