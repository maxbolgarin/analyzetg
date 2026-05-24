"""Tests for `_forum_pick_mode` — fixes the bot's forum-link hang.

When the user sends a forum link to the bot, `cmd_analyze` lands on
`_forum_pick_mode`. The legacy implementation showed an interactive
terminal picker — which blocks the bot forever because the user
can't reach the terminal from Telegram. `yes=True` now short-circuits
that to "all-flat" (analyze the whole forum as one chat).
"""

from __future__ import annotations

import pytest

from unread.analyzer.commands import _forum_pick_mode
from unread.tg.topics import ForumTopic


@pytest.mark.asyncio
async def test_forum_pick_mode_with_yes_defaults_to_all_flat(monkeypatch):
    """Bot mode (yes=True) → no prompt, returns all_flat=True without blocking."""

    async def fake_topics(_client, _chat_id):
        return [
            ForumTopic(topic_id=1, title="General", unread_count=0, read_inbox_max_id=0),
            ForumTopic(topic_id=8, title="Chat", unread_count=0, read_inbox_max_id=0),
        ]

    monkeypatch.setattr("unread.analyzer.commands.list_forum_topics", fake_topics)
    all_flat, all_per_topic, thread_id = await _forum_pick_mode(
        client=None,
        chat_id=12345,
        chat_title="Test Forum",
        yes=True,
    )
    assert all_flat is True
    assert all_per_topic is False
    assert thread_id == 0


@pytest.mark.asyncio
async def test_forum_pick_mode_with_no_topics_exits_cleanly(monkeypatch):
    """Empty forum → typer.Exit(0). yes flag doesn't change this."""
    import typer

    async def fake_topics(_client, _chat_id):
        return []

    monkeypatch.setattr("unread.analyzer.commands.list_forum_topics", fake_topics)
    with pytest.raises(typer.Exit) as exc_info:
        await _forum_pick_mode(client=None, chat_id=1, chat_title="X", yes=True)
    assert exc_info.value.exit_code == 0
