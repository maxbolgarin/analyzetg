"""Pre-prod hardening: `_seed_home_templates` writes 0o600 from creation
for both `.env` and `config.toml`.

Without `secret_write_text`, `shutil.copyfile` opens the destination
with the user's umask (typically 0o644 → group/world readable). The
follow-up `chmod` shrinks the window but doesn't close it on a
multi-user host.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_seed_home_templates_writes_0600_for_both_files(tmp_path, monkeypatch):
    """Both seeded files (.env + config.toml) must be 0o600 immediately."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("UNREAD_HOME", str(home))

    from unread.cli import _seed_home_templates
    from unread.config import reset_settings

    reset_settings()
    # Make sure neither template exists yet — _seed only seeds when absent.
    env_target = home / ".env"
    cfg_target = home / "config.toml"
    assert not env_target.exists()
    assert not cfg_target.exists()

    _seed_home_templates()

    # Both must exist…
    assert env_target.exists(), "_seed_home_templates didn't seed .env"
    assert cfg_target.exists(), "_seed_home_templates didn't seed config.toml"
    # …with mode 0o600 (no group/world bits, owner read+write).
    env_mode = os.stat(env_target).st_mode & 0o777
    cfg_mode = os.stat(cfg_target).st_mode & 0o777
    assert env_mode == 0o600, f".env mode is {oct(env_mode)}, expected 0o600"
    assert cfg_mode == 0o600, f"config.toml mode is {oct(cfg_mode)}, expected 0o600"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_seed_home_templates_idempotent(tmp_path, monkeypatch):
    """Re-running with files present is a no-op (no overwrite)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("UNREAD_HOME", str(home))

    from unread.cli import _seed_home_templates
    from unread.config import reset_settings

    reset_settings()

    _seed_home_templates()
    env_target = home / ".env"
    cfg_target = home / "config.toml"
    env_inode_first = env_target.stat().st_ino
    cfg_inode_first = cfg_target.stat().st_ino

    # Touch the files to detect a re-write
    env_target.write_text("user-edit-1", encoding="utf-8")
    cfg_target.write_text("user-edit-2", encoding="utf-8")
    Path(env_target).chmod(0o600)
    Path(cfg_target).chmod(0o600)

    _seed_home_templates()

    # Same inode (file wasn't replaced) and content kept the user edit.
    assert env_target.stat().st_ino == env_inode_first
    assert cfg_target.stat().st_ino == cfg_inode_first
    assert env_target.read_text(encoding="utf-8") == "user-edit-1"
    assert cfg_target.read_text(encoding="utf-8") == "user-edit-2"
