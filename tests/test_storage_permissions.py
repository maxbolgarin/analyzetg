"""Sensitive on-disk artifacts are tightened to 0o600 on creation.

The home directory is already 0o700, but the SQLite DB, Telethon session
file, and report Markdown inside it inherit the process umask (typically
0o644) when their respective libraries create them. On a multi-user box
that's world-readable secrets / private chat content. Each writer calls
``unread.util.fsmode.tighten`` after creation; these tests pin the
behaviour so a regression on any writer is loud.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from unread.db.repo import Repo
from unread.util.fsmode import SECRET_FILE_MODE, tighten


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics only")
@pytest.mark.asyncio
async def test_repo_open_tightens_data_sqlite(tmp_path):
    db = tmp_path / "data.sqlite"
    repo = await Repo.open(db)
    try:
        assert _mode(db) == SECRET_FILE_MODE
    finally:
        await repo.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics only")
def test_tighten_helper_sets_owner_only(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("secret")
    f.chmod(0o644)
    assert tighten(f) is True
    assert _mode(f) == SECRET_FILE_MODE


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics only")
def test_tighten_returns_false_for_missing_file(tmp_path):
    f = tmp_path / "ghost.txt"
    # Logs warning, returns False, never raises.
    assert tighten(f) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics only")
@pytest.mark.asyncio
async def test_repo_open_tightens_wal_sibling_when_present(tmp_path):
    db = tmp_path / "data.sqlite"
    repo = await Repo.open(db)
    try:
        # Force WAL by writing something so the -wal sibling materializes.
        await repo.set_app_setting("locale.language", "en")
    finally:
        await repo.close()
    wal = db.with_suffix(db.suffix + "-wal")
    if wal.exists():  # WAL materialization is OS-dependent
        # Pre-loosen so we can verify tighten actually runs on re-open.
        os.chmod(wal, 0o644)
        repo2 = await Repo.open(db)
        try:
            assert _mode(wal) == SECRET_FILE_MODE
        finally:
            await repo2.close()
