"""SDK auth errors get rewrapped as friendly ProviderUnavailableError.

Without this, a user with a stale / revoked / typo'd API key sees a raw
``openai.AuthenticationError`` (or the equivalent from Anthropic /
Google / OpenRouter) which doesn't tell them what to do. The
chat_complete wrapper catches anything auth-shaped and surfaces a
one-line "Run `unread tg init`" hint.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from unread.ai.providers import ProviderUnavailableError
from unread.analyzer.openai_client import _is_auth_error, chat_complete


class _FakeAuthError(Exception):
    """Mimics openai.AuthenticationError / anthropic.AuthenticationError shape."""

    pass


_FakeAuthError.__name__ = "AuthenticationError"


class _StatusError(Exception):
    def __init__(self, msg: str, status: int):
        super().__init__(msg)
        self.status_code = status


def test_is_auth_error_classname():
    assert _is_auth_error("openai", _FakeAuthError("invalid key"))


def test_is_auth_error_status_401():
    assert _is_auth_error("openai", _StatusError("nope", 401))


def test_is_auth_error_status_403():
    assert _is_auth_error("anthropic", _StatusError("forbidden", 403))


def test_is_auth_error_negatives():
    assert _is_auth_error("openai", RuntimeError("network down")) is False
    assert _is_auth_error("openai", _StatusError("rate limited", 429)) is False
    assert _is_auth_error("openai", _StatusError("server", 500)) is False


@pytest.mark.asyncio
async def test_chat_complete_remaps_auth_error_to_friendly():
    provider = MagicMock()
    provider.name = "openai"
    provider.chat = AsyncMock(side_effect=_FakeAuthError("invalid key"))

    repo = MagicMock()
    repo.log_usage = AsyncMock()

    with pytest.raises(ProviderUnavailableError) as ei:
        await chat_complete(
            provider,
            repo=repo,
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
        )
    msg = str(ei.value)
    assert "openai" in msg.lower()
    assert "rejected" in msg.lower() or "invalid" in msg.lower()
    assert "unread tg init" in msg


@pytest.mark.asyncio
async def test_chat_complete_passes_through_non_auth_errors():
    provider = MagicMock()
    provider.name = "anthropic"
    provider.chat = AsyncMock(side_effect=RuntimeError("network down"))

    repo = MagicMock()
    repo.log_usage = AsyncMock()

    with pytest.raises(RuntimeError, match="network down"):
        await chat_complete(
            provider,
            repo=repo,
            model="claude-test",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
        )
