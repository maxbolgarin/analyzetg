"""ask wizard dispatch: collect answers → call cmd_ask with mapped kwargs."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_cmd_ask_with_question_no_scope_routes_to_wizard():
    """`atg ask "Q"` (question, no scope) → wizard, NOT global retrieval.

    Spec: no scope flag set → wizard, regardless of whether the question
    is supplied. The wizard collects scope; the question is forwarded.
    """
    from analyzetg.ask import commands as ask_commands

    fake_wizard = AsyncMock()
    # `run_interactive_ask` is imported lazily inside cmd_ask, so patch it
    # at the source module — the lazy import resolves at call time.
    with patch("analyzetg.interactive.run_interactive_ask", new=fake_wizard):
        await ask_commands.cmd_ask(
            question="как дела?",
            ref=None,
            chat=None,
            folder=None,
            global_scope=False,
        )

    fake_wizard.assert_awaited_once()
    assert fake_wizard.call_args.kwargs["question"] == "как дела?"


@pytest.mark.asyncio
async def test_run_interactive_ask_calls_cmd_ask_with_global_when_all_local_picked():
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
        mark_read=False,
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

    fake_cmd.assert_awaited_once()
    kwargs = fake_cmd.call_args.kwargs
    assert kwargs["question"] == "что нового?"
    assert kwargs["global_scope"] is True
    assert kwargs["chat"] is None
    assert kwargs["thread"] is None


@pytest.mark.asyncio
async def test_run_interactive_ask_passes_chat_id_and_thread_when_chat_picked():
    from analyzetg.interactive import InteractiveAnswers, run_interactive_ask

    answers = InteractiveAnswers(
        chat_ref="-1001234567890",
        chat_kind="forum",
        thread_id=42,
        forum_all_flat=False,
        forum_all_per_topic=False,
        preset=None,
        period="last7",
        custom_since=None,
        custom_until=None,
        console_out=False,
        mark_read=False,
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
    assert kwargs["question"] == "open Qs?"
    assert kwargs["chat"] == "-1001234567890"
    assert kwargs["thread"] == 42
    assert kwargs["last_days"] == 7
    assert kwargs["global_scope"] is False
