"""Positional <ref> resolves to chat/thread/msg correctly for ask."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from atg.ask.commands import _resolve_ask_ref
from atg.models import ResolvedRef


@pytest.mark.asyncio
async def test_resolve_topic_url_returns_chat_and_thread():
    """t.me/c/<id>/<topic> → chat_id=-100<id>, thread_id=<topic>."""
    fake_client = MagicMock()
    fake_repo = MagicMock()

    async def fake_resolve(client, repo, ref):
        return ResolvedRef(
            chat_id=-1003865481227,
            kind="forum",
            title="Some Forum",
            username=None,
            thread_id=4,
            msg_id=None,
        )

    chat_id, thread_id, msg_id = await _resolve_ask_ref(
        fake_client,
        fake_repo,
        "https://t.me/c/3865481227/4",
        resolve_fn=fake_resolve,
    )
    assert chat_id == -1003865481227
    assert thread_id == 4
    assert msg_id is None


@pytest.mark.asyncio
async def test_resolve_username_returns_chat_only():
    """`@user` → chat_id only; thread_id and msg_id are None."""
    fake_client = MagicMock()
    fake_repo = MagicMock()

    async def fake_resolve(client, repo, ref):
        return ResolvedRef(
            chat_id=12345,
            kind="user",
            title="Bob",
            username="bob",
            thread_id=None,
            msg_id=None,
        )

    chat_id, thread_id, msg_id = await _resolve_ask_ref(
        fake_client,
        fake_repo,
        "@bob",
        resolve_fn=fake_resolve,
    )
    assert chat_id == 12345
    assert thread_id is None
    assert msg_id is None
