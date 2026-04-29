"""Doctor flags overpermissive `~/.unread/storage/` modes on POSIX.

The DB is unencrypted; storage permissions are the only protection
against another local user reading saved Telegram + LLM credentials.
This guard catches installs migrated from a release before
`ensure_unread_home()` chmod'd 0o700.
"""

from __future__ import annotations

import os
from io import StringIO

import pytest
from rich.console import Console


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only permission semantics")
@pytest.mark.asyncio
async def test_doctor_warns_on_world_readable_storage(tmp_path, monkeypatch):
    """A 0o755 storage dir / 0o644 data.sqlite triggers a WARN line with
    a copy-pasteable `chmod` fix."""
    install = tmp_path / "unread"
    storage = install / "storage"
    storage.mkdir(parents=True)
    db = storage / "data.sqlite"
    db.write_bytes(b"")
    # Reproduce the pre-hardening permissions.
    storage.chmod(0o755)
    db.chmod(0o644)

    monkeypatch.setenv("UNREAD_HOME", str(install))
    from unread.config import reset_settings

    reset_settings()

    # Capture doctor output.
    buf = StringIO()
    captured = Console(file=buf, force_terminal=False, width=120)
    import unread.tg.commands as tg_cmds

    saved = tg_cmds.console
    tg_cmds.console = captured  # type: ignore[assignment]
    try:
        # `typer.Exit(1)` from missing creds is fine — we only care
        # about the line we just added.
        import contextlib

        with contextlib.suppress(SystemExit, RuntimeError):
            await tg_cmds.cmd_doctor()
    finally:
        tg_cmds.console = saved  # type: ignore[assignment]

    out = buf.getvalue()
    assert "storage permissions overpermissive" in out
    assert "chmod 700" in out and "chmod 600" in out
