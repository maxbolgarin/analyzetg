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
    # Friendly banner copy, not a traceback.
    assert "Telegram session" in captured.out
    assert "tg init --force" in captured.out
    assert "Traceback" not in captured.out


def test_session_expired_inherits_runtime_error() -> None:
    """Subclass of RuntimeError so existing `except RuntimeError` paths still catch it."""
    assert issubclass(TelegramSessionExpired, RuntimeError)
