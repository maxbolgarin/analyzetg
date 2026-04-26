"""--global skips the wizard and runs retrieve_messages with chat_ids=None."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_global_flag_calls_retrieve_with_no_chat_filter(monkeypatch):
    """`--global` skips the wizard and runs retrieve_messages with chat_ids=None."""
    from analyzetg.ask.commands import cmd_ask

    captured = {"called": False}

    async def fake_retrieve(*, repo, question, chat_ids, **kwargs):
        captured["called"] = True
        captured["chat_ids"] = chat_ids
        return []

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

        # Empty retrieval → typer.Exit downstream; we only care that the
        # call site was reached with the correct chat_ids filter.
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

    assert captured["called"] is True, "retrieve_messages was never reached"
    assert captured["chat_ids"] is None  # global / all chats
