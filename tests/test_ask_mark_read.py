"""ask + mark-read: CLI flag and wizard step.

Covers the four behavioral surfaces:
  (a) `--mark-read` + single-chat scope → calls `mark_as_read` with the
      highest msg_id from the retrieved pool.
  (b) `--no-mark-read` (mark_read=False) is a no-op.
  (c) `--mark-read --global` (or any non-single-chat scope) is a no-op —
      there's no single chat to mark.
  (d) Default `mark_read=None` is a no-op.

The wizard-forwarding case is in `test_ask_wizard_dispatch.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from analyzetg.models import Message, ResolvedRef


def _msg(chat_id: int, msg_id: int) -> Message:
    return Message(
        chat_id=chat_id,
        msg_id=msg_id,
        date=datetime(2024, 1, 1, tzinfo=UTC),
        text=f"hello {msg_id}",
    )


@pytest.mark.asyncio
async def test_mark_read_calls_send_read_acknowledge_on_chat_scope():
    """`atg ask "Q" --chat=@x --mark-read` calls send_read_acknowledge with
    the resolved chat_id and the highest msg_id from the retrieved pool."""
    from analyzetg.ask import commands as ask_commands

    fake_client = MagicMock()
    fake_client.send_read_acknowledge = AsyncMock(return_value=None)

    fake_repo = AsyncMock()
    fake_repo.get_chat = AsyncMock(return_value=None)

    pool = [(_msg(123, 100), 90), (_msg(123, 12345), 95), (_msg(123, 7000), 80)]

    async def fake_run_single(**kwargs):
        # Mimic the real shape: returns (answer_text, scored_pool).
        return "answer", pool

    async def fake_resolve(client, repo, ref):
        return ResolvedRef(chat_id=123, kind="user", title="X", username="x")

    with (
        patch.object(ask_commands, "_run_single_turn", new=fake_run_single),
        patch.object(ask_commands, "resolve_ref", new=fake_resolve),
        patch.object(ask_commands, "tg_client") as fake_tg,
        patch.object(ask_commands, "open_repo") as fake_open_repo,
    ):
        fake_tg.return_value.__aenter__ = AsyncMock(return_value=fake_client)
        fake_tg.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_open_repo.return_value.__aenter__ = AsyncMock(return_value=fake_repo)
        fake_open_repo.return_value.__aexit__ = AsyncMock(return_value=False)

        await ask_commands.cmd_ask(
            question="hello",
            ref=None,
            chat="@x",
            folder=None,
            global_scope=False,
            no_followup=True,
            mark_read=True,
        )

    fake_client.send_read_acknowledge.assert_awaited_once()
    args, kwargs = fake_client.send_read_acknowledge.call_args
    # Telethon: client.send_read_acknowledge(chat_id, max_id=...).
    assert args[0] == 123
    assert kwargs.get("max_id") == 12345  # max msg_id in pool


@pytest.mark.asyncio
async def test_no_mark_read_does_not_call_send_read_acknowledge():
    """`mark_read=False` → never call send_read_acknowledge."""
    from analyzetg.ask import commands as ask_commands

    fake_client = MagicMock()
    fake_client.send_read_acknowledge = AsyncMock(return_value=None)

    fake_repo = AsyncMock()
    fake_repo.get_chat = AsyncMock(return_value=None)

    async def fake_run_single(**kwargs):
        return "answer", [(_msg(123, 99), 50)]

    async def fake_resolve(client, repo, ref):
        return ResolvedRef(chat_id=123, kind="user", title="X", username="x")

    with (
        patch.object(ask_commands, "_run_single_turn", new=fake_run_single),
        patch.object(ask_commands, "resolve_ref", new=fake_resolve),
        patch.object(ask_commands, "tg_client") as fake_tg,
        patch.object(ask_commands, "open_repo") as fake_open_repo,
    ):
        fake_tg.return_value.__aenter__ = AsyncMock(return_value=fake_client)
        fake_tg.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_open_repo.return_value.__aenter__ = AsyncMock(return_value=fake_repo)
        fake_open_repo.return_value.__aexit__ = AsyncMock(return_value=False)

        await ask_commands.cmd_ask(
            question="hello",
            ref=None,
            chat="@x",
            folder=None,
            global_scope=False,
            no_followup=True,
            mark_read=False,
        )

    fake_client.send_read_acknowledge.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_read_default_none_does_not_call():
    """When `mark_read=None` (default — flag not passed), do not mark read."""
    from analyzetg.ask import commands as ask_commands

    fake_client = MagicMock()
    fake_client.send_read_acknowledge = AsyncMock(return_value=None)

    fake_repo = AsyncMock()
    fake_repo.get_chat = AsyncMock(return_value=None)

    async def fake_run_single(**kwargs):
        return "answer", [(_msg(123, 99), 50)]

    async def fake_resolve(client, repo, ref):
        return ResolvedRef(chat_id=123, kind="user", title="X", username="x")

    with (
        patch.object(ask_commands, "_run_single_turn", new=fake_run_single),
        patch.object(ask_commands, "resolve_ref", new=fake_resolve),
        patch.object(ask_commands, "tg_client") as fake_tg,
        patch.object(ask_commands, "open_repo") as fake_open_repo,
    ):
        fake_tg.return_value.__aenter__ = AsyncMock(return_value=fake_client)
        fake_tg.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_open_repo.return_value.__aenter__ = AsyncMock(return_value=fake_repo)
        fake_open_repo.return_value.__aexit__ = AsyncMock(return_value=False)

        await ask_commands.cmd_ask(
            question="hello",
            ref=None,
            chat="@x",
            folder=None,
            global_scope=False,
            no_followup=True,
            # mark_read omitted → defaults to None
        )

    fake_client.send_read_acknowledge.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_read_is_noop_with_global_scope():
    """`--mark-read --global` is a silent no-op (no single chat to mark)."""
    from analyzetg.ask import commands as ask_commands

    fake_client = MagicMock()
    fake_client.send_read_acknowledge = AsyncMock(return_value=None)

    fake_repo = AsyncMock()
    fake_repo.get_chat = AsyncMock(return_value=None)

    async def fake_run_single(**kwargs):
        # Two chats in the pool — global scope.
        return "answer", [(_msg(111, 50), 70), (_msg(222, 80), 60)]

    with (
        patch.object(ask_commands, "_run_single_turn", new=fake_run_single),
        patch.object(ask_commands, "tg_client") as fake_tg,
        patch.object(ask_commands, "open_repo") as fake_open_repo,
    ):
        fake_tg.return_value.__aenter__ = AsyncMock(return_value=fake_client)
        fake_tg.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_open_repo.return_value.__aenter__ = AsyncMock(return_value=fake_repo)
        fake_open_repo.return_value.__aexit__ = AsyncMock(return_value=False)

        await ask_commands.cmd_ask(
            question="hello",
            ref=None,
            chat=None,
            folder=None,
            global_scope=True,
            no_followup=True,
            mark_read=True,  # explicitly set; should still be ignored
        )

    fake_client.send_read_acknowledge.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_read_falls_back_to_max_msg_id_when_pool_empty():
    """When the LLM saw a non-empty pool but the prior_pool fallback to
    `repo.get_max_msg_id` is exercised — i.e. the pool is empty after the
    final follow-up turn — we still mark read using the chat's local max."""
    from analyzetg.ask import commands as ask_commands

    fake_client = MagicMock()
    fake_client.send_read_acknowledge = AsyncMock(return_value=None)

    fake_repo = AsyncMock()
    fake_repo.get_chat = AsyncMock(return_value=None)
    fake_repo.get_max_msg_id = AsyncMock(return_value=999)

    async def fake_run_single(**kwargs):
        # Empty pool — caller's prior_pool stays []; mark-read should
        # fall back to repo.get_max_msg_id.
        return "answer", []

    async def fake_resolve(client, repo, ref):
        return ResolvedRef(chat_id=555, kind="user", title="Y", username="y")

    with (
        patch.object(ask_commands, "_run_single_turn", new=fake_run_single),
        patch.object(ask_commands, "resolve_ref", new=fake_resolve),
        patch.object(ask_commands, "tg_client") as fake_tg,
        patch.object(ask_commands, "open_repo") as fake_open_repo,
    ):
        fake_tg.return_value.__aenter__ = AsyncMock(return_value=fake_client)
        fake_tg.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_open_repo.return_value.__aenter__ = AsyncMock(return_value=fake_repo)
        fake_open_repo.return_value.__aexit__ = AsyncMock(return_value=False)

        await ask_commands.cmd_ask(
            question="hello",
            ref=None,
            chat="@y",
            folder=None,
            global_scope=False,
            no_followup=True,
            mark_read=True,
        )

    fake_client.send_read_acknowledge.assert_awaited_once()
    args, kwargs = fake_client.send_read_acknowledge.call_args
    assert args[0] == 555
    assert kwargs.get("max_id") == 999


@pytest.mark.asyncio
async def test_mark_read_failure_does_not_abort():
    """If `send_read_acknowledge` raises, ask still returns cleanly —
    the answer is on screen and a mark-read failure must not surface."""
    from analyzetg.ask import commands as ask_commands

    fake_client = MagicMock()
    fake_client.send_read_acknowledge = AsyncMock(side_effect=RuntimeError("boom"))

    fake_repo = AsyncMock()
    fake_repo.get_chat = AsyncMock(return_value=None)

    async def fake_run_single(**kwargs):
        return "answer", [(_msg(123, 100), 50)]

    async def fake_resolve(client, repo, ref):
        return ResolvedRef(chat_id=123, kind="user", title="X", username="x")

    with (
        patch.object(ask_commands, "_run_single_turn", new=fake_run_single),
        patch.object(ask_commands, "resolve_ref", new=fake_resolve),
        patch.object(ask_commands, "tg_client") as fake_tg,
        patch.object(ask_commands, "open_repo") as fake_open_repo,
    ):
        fake_tg.return_value.__aenter__ = AsyncMock(return_value=fake_client)
        fake_tg.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_open_repo.return_value.__aenter__ = AsyncMock(return_value=fake_repo)
        fake_open_repo.return_value.__aexit__ = AsyncMock(return_value=False)

        # Must not raise.
        await ask_commands.cmd_ask(
            question="hello",
            ref=None,
            chat="@x",
            folder=None,
            global_scope=False,
            no_followup=True,
            mark_read=True,
        )


@pytest.mark.asyncio
async def test_wizard_forwards_mark_read_to_cmd_ask():
    """`run_interactive_ask` honours InteractiveAnswers.mark_read and
    threads it into cmd_ask when a single chat is picked."""
    from analyzetg.interactive import InteractiveAnswers, run_interactive_ask

    answers = InteractiveAnswers(
        chat_ref="-1001234567890",
        chat_kind="supergroup",
        thread_id=None,
        forum_all_flat=False,
        forum_all_per_topic=False,
        preset=None,
        period="last7",
        custom_since=None,
        custom_until=None,
        console_out=False,
        mark_read=True,  # picked "yes" in the wizard
        output_path=None,
        run_on_all_unread=False,
        run_on_all_local=False,
        enrich_kinds=None,
        custom_from_msg=None,
        with_comments=False,
    )

    with (
        patch("analyzetg.interactive._collect_answers", new=AsyncMock(return_value=answers)),
        patch("analyzetg.ask.commands.cmd_ask", new=AsyncMock()) as fake_cmd,
    ):
        await run_interactive_ask(question="open Qs?")

    kwargs = fake_cmd.call_args.kwargs
    assert kwargs["mark_read"] is True


@pytest.mark.asyncio
async def test_wizard_does_not_forward_mark_read_for_all_local():
    """ALL_LOCAL has no single chat → mark_read forced to None even if
    answers.mark_read leaks True. (Defensive guard against the wizard
    skipping the step but leaving the field at its default.)"""
    from analyzetg.interactive import InteractiveAnswers, run_interactive_ask

    answers = InteractiveAnswers(
        chat_ref="",
        chat_kind="",
        thread_id=None,
        forum_all_flat=False,
        forum_all_per_topic=False,
        preset=None,
        period="unread",
        custom_since=None,
        custom_until=None,
        console_out=False,
        mark_read=True,  # would be a bug, but the wizard might leave it
        output_path=None,
        run_on_all_unread=False,
        run_on_all_local=True,
        enrich_kinds=None,
        custom_from_msg=None,
        with_comments=False,
    )

    with (
        patch("analyzetg.interactive._collect_answers", new=AsyncMock(return_value=answers)),
        patch("analyzetg.ask.commands.cmd_ask", new=AsyncMock()) as fake_cmd,
    ):
        await run_interactive_ask(question="что нового?")

    kwargs = fake_cmd.call_args.kwargs
    assert kwargs["mark_read"] is None
