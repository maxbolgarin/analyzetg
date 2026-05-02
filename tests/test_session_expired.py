"""Telegram session-expired handling.

When `is_user_authorized()` returns False on a session file that
exists, `tg_client` raises `TelegramSessionExpired`. The top-level
`_run` boundary in `unread/cli.py` should catch that and turn it into
a friendly banner + `typer.Exit(1)` instead of a raw traceback.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from unread.tg.client import TelegramSessionExpired


@pytest.mark.asyncio
async def test_tg_client_raises_typed_exception_when_unauthorized(monkeypatch) -> None:
    """Bare-bones contract: tg_client raises TelegramSessionExpired (subclass of RuntimeError)."""
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.disconnect = AsyncMock()
    fake_client.is_user_authorized = AsyncMock(return_value=False)

    with patch("unread.tg.client.build_client", return_value=fake_client):
        from unread.tg.client import tg_client

        with pytest.raises(TelegramSessionExpired):
            async with tg_client(require_auth=True):
                pass

    # And we still hit `disconnect` so we don't leak the connection.
    fake_client.disconnect.assert_awaited()


def test_run_converts_session_expired_to_friendly_exit(capsys) -> None:
    """`_run(coro_that_raises_session_expired)` exits cleanly, not with a traceback."""
    from unread.cli import _run

    async def _coro():
        raise TelegramSessionExpired("test")

    with pytest.raises(typer.Exit) as exc_info:
        _run(_coro())
    assert exc_info.value.exit_code == 1
    captured = capsys.readouterr()
    # Friendly banner copy, not a traceback. The hint points at
    # `unread login --force` (was `tg init --force` before the
    # `tg` subgroup was retired in favor of magic-ref dispatch).
    assert "Telegram session" in captured.out
    assert "login --force" in captured.out
    assert "Traceback" not in captured.out


def test_session_expired_inherits_runtime_error() -> None:
    """Subclass of RuntimeError so existing `except RuntimeError` paths still catch it."""
    assert issubclass(TelegramSessionExpired, RuntimeError)


def test_exit_session_expired_wipes_local_session_file(tmp_path, monkeypatch) -> None:
    """Regression: after the friendly banner, the local session file must
    be gone so the next ``unread help`` doesn't keep reporting "session
    linked" for a server-side-revoked session.

    Before this fix, ``is_session_authorized_sync`` (which only checks
    for a non-NULL ``auth_key`` on disk) kept returning True after a
    session-expired error — confusing users who saw "✓ session linked"
    in status one second after being told "session expired".
    """
    import sqlite3

    from unread.config import reset_settings
    from unread.tg.client import exit_session_expired

    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    reset_settings()

    from unread.config import get_settings

    s = get_settings()
    session_path = s.telegram.session_path
    session_path.parent.mkdir(parents=True, exist_ok=True)

    # Build a minimal Telethon-shaped session file so the wipe path has
    # something to delete. Just an empty SQLite file at the path is
    # enough — `_wipe_local_session` uses `Path.unlink`.
    real_file = session_path.with_name(session_path.name + ".session")
    conn = sqlite3.connect(str(real_file))
    conn.execute("CREATE TABLE sessions (auth_key BLOB)")
    conn.execute("INSERT INTO sessions(auth_key) VALUES(X'deadbeef')")
    conn.commit()
    conn.close()

    assert real_file.exists()

    with pytest.raises(typer.Exit):
        exit_session_expired()

    assert not real_file.exists(), "session file should have been wiped"
    assert not session_path.exists(), "primary session path should also be gone"


def test_exit_session_expired_clears_passphrase_session_string(tmp_path, monkeypatch) -> None:
    """Passphrase-backend variant: clear the encrypted ``telegram.session_string``
    slot so the next status read accurately reports "not linked"."""
    import sqlite3

    from unread.config import reset_settings
    from unread.tg.client import exit_session_expired

    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    reset_settings()

    from unread.config import get_settings

    s = get_settings()
    db_path = s.storage.data_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Bare schema: just the tables `_wipe_local_session` reads + the
    # backend flag in `app_settings`. No need to load the full schema.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        CREATE TABLE secrets (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        INSERT INTO app_settings(key, value, updated_at) VALUES ('secrets.backend', 'passphrase', '2026-01-01');
        INSERT INTO secrets(key, value, updated_at) VALUES ('telegram.session_string', 'somecipher', '2026-01-01');
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(typer.Exit):
        exit_session_expired()

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT value FROM secrets WHERE key = ?", ("telegram.session_string",)).fetchall()
    conn.close()
    assert rows == [], "encrypted session-string slot should have been cleared"
