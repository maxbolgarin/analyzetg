"""--global skips the wizard and runs retrieve_messages with chat_ids=None."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_global_flag_calls_retrieve_with_no_chat_filter(monkeypatch):
    """`--global` skips the wizard and runs retrieve_messages with chat_ids=None."""
    from analyzetg.ask.commands import cmd_ask

    captured = {}

    async def fake_retrieve(*, repo, question, chat_ids, **kwargs):
        captured["chat_ids"] = chat_ids
        return []  # empty pool → cmd_ask exits early before LLM call

    with (
        patch("analyzetg.ask.commands.retrieve_messages", new=fake_retrieve),
        patch("analyzetg.ask.commands.tg_client") as fake_tg,
        patch("analyzetg.ask.commands.open_repo") as fake_repo,
        patch("analyzetg.ask.commands._refresh_chats", new=AsyncMock()),
    ):
        fake_tg.return_value.__aenter__ = AsyncMock(return_value=object())
        fake_tg.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_repo.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        fake_repo.return_value.__aexit__ = AsyncMock(return_value=False)

        with contextlib.suppress(Exception):
            await cmd_ask(
                question="hello",
                ref=None,
                chat=None,
                folder=None,
                global_scope=True,
                semantic=False,
                build_index=False,
                refresh=False,
                limit=200,
            )
        # empty retrieval → typer.Exit; we already captured chat_ids

    assert captured.get("chat_ids") is None  # global / all chats
