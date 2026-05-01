"""`cmd_ask` skips the Telegram open when no TG-needing flag is set.

Pre-fix, `unread ask "Q"` against the local archive (no `--chat`,
no `--folder`, no `--refresh`) still opened a Telegram session in the
context-manager line and bombed with "Telegram session expired or
invalid" when the session was bad — even though no Telegram RPC was
about to be issued. This pins the new behavior: in the local-only path
`tg_client` is never called.

The complementary "still opens TG when `--chat` is set" path is
already exercised by `test_ask_mark_read.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from unread.models import Message


def _msg(chat_id: int, msg_id: int) -> Message:
    return Message(
        chat_id=chat_id,
        msg_id=msg_id,
        date=datetime(2024, 1, 1, tzinfo=UTC),
        text=f"hello {msg_id}",
    )


@pytest.mark.asyncio
async def test_local_archive_ask_does_not_open_tg_client():
    """`unread ask "Q" --global` (or any no-chat/no-folder path) must not
    enter `tg_client`. The poison-pill double-checks the contract: if
    something inside cmd_ask still opens it, the test crashes loudly
    instead of silently passing."""
    from unread.ask import commands as ask_commands

    fake_repo = AsyncMock()
    fake_repo.get_chat = AsyncMock(return_value=None)

    async def fake_run_single(**kwargs):
        return "answer", [(_msg(123, 1), 50)]

    poison = MagicMock(side_effect=AssertionError("tg_client must not be opened for a local-only ask"))

    with (
        patch.object(ask_commands, "_run_single_turn", new=fake_run_single),
        patch.object(ask_commands, "tg_client", new=poison),
        patch.object(ask_commands, "open_repo") as fake_open_repo,
    ):
        fake_open_repo.return_value.__aenter__ = AsyncMock(return_value=fake_repo)
        fake_open_repo.return_value.__aexit__ = AsyncMock(return_value=False)

        await ask_commands.cmd_ask(
            question="hello",
            ref=None,
            chat=None,
            folder=None,
            global_scope=True,
            no_followup=True,
        )

    poison.assert_not_called()


def test_ask_needs_tg_helper_matches_documented_rule():
    """The single helper that decides "open TG or not" — sanity check
    the truth table so a future refactor that adds a flag has a
    predictable failure mode."""
    from unread.ask.commands import _ask_needs_tg

    # Local-only: no chat, no folder.
    assert _ask_needs_tg(chat=None, folder=None) is False
    # `--chat` set → needs TG (resolve_ref + maybe enrichment).
    assert _ask_needs_tg(chat="@x", folder=None) is True
    # `--folder` set → needs TG (list_folders).
    assert _ask_needs_tg(chat=None, folder="Work") is True
    # Both set → still needs TG.
    assert _ask_needs_tg(chat="@x", folder="Work") is True
