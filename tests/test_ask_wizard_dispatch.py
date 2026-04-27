"""ask wizard dispatch: collect answers → call cmd_ask with mapped kwargs."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_cmd_ask_with_question_no_scope_routes_to_wizard():
    """`unread ask "Q"` (question, no scope) → wizard, NOT global retrieval.

    Spec: no scope flag set → wizard, regardless of whether the question
    is supplied. The wizard collects scope; the question is forwarded.
    """
    from unread.ask import commands as ask_commands

    fake_wizard = AsyncMock()
    # `run_interactive_ask` is imported lazily inside cmd_ask, so patch it
    # at the source module — the lazy import resolves at call time.
    with patch("unread.interactive.run_interactive_ask", new=fake_wizard):
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
    from unread.interactive import InteractiveAnswers, run_interactive_ask

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
        patch("unread.interactive._collect_answers", new=AsyncMock(return_value=answers)),
        patch("unread.ask.commands.cmd_ask", new=AsyncMock()) as fake_cmd,
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
    from unread.interactive import InteractiveAnswers, run_interactive_ask

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
        # User picked "yes" in the new ask-mode mark-read step.
        mark_read=True,
        output_path=None,
        run_on_all_unread=False,
        run_on_all_local=False,
        # User picked "voice" in the wizard's enrich step — the wizard
        # forwards that to cmd_ask as --enrich=voice.
        enrich_kinds=["voice"],
        custom_from_msg=None,
        with_comments=False,
    )

    with (
        patch("unread.interactive._collect_answers", new=AsyncMock(return_value=answers)),
        patch("unread.ask.commands.cmd_ask", new=AsyncMock()) as fake_cmd,
    ):
        await run_interactive_ask(question="open Qs?")

    kwargs = fake_cmd.call_args.kwargs
    assert kwargs["question"] == "open Qs?"
    assert kwargs["chat"] == "-1001234567890"
    assert kwargs["thread"] == 42
    assert kwargs["last_days"] == 7
    assert kwargs["global_scope"] is False
    # Wizard mode auto-refreshes the picked chat — the user just stepped
    # through a flow expecting fresh answers, not stale local data.
    assert kwargs["refresh"] is True
    # Wizard enrich kinds → cmd_ask --enrich CSV.
    assert kwargs["enrich"] == "voice"
    assert kwargs["no_enrich"] is False
    # Wizard's mark-read pick threads into cmd_ask.
    assert kwargs["mark_read"] is True


@pytest.mark.asyncio
async def test_run_interactive_ask_passes_last_hours_for_last24h():
    """Wizard's `last24h` period option threads into cmd_ask as last_hours=24.

    Regression guard that the new hour-granular options are wired to the
    new --last-hours flag (not silently dropped or coerced into days).
    """
    from unread.interactive import InteractiveAnswers, run_interactive_ask

    answers = InteractiveAnswers(
        chat_ref="-1001234567890",
        chat_kind="supergroup",
        thread_id=None,
        forum_all_flat=False,
        forum_all_per_topic=False,
        preset=None,
        period="last24h",
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
        patch("unread.interactive._collect_answers", new=AsyncMock(return_value=answers)),
        patch("unread.ask.commands.cmd_ask", new=AsyncMock()) as fake_cmd,
    ):
        await run_interactive_ask(question="recent news?")

    kwargs = fake_cmd.call_args.kwargs
    assert kwargs["last_hours"] == 24
    assert kwargs.get("last_days") is None


@pytest.mark.asyncio
async def test_run_interactive_ask_does_not_force_refresh_for_all_local():
    """ALL_LOCAL is an explicit local-only path — no Telegram backfill."""
    from unread.interactive import InteractiveAnswers, run_interactive_ask

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
        patch("unread.interactive._collect_answers", new=AsyncMock(return_value=answers)),
        patch("unread.ask.commands.cmd_ask", new=AsyncMock()) as fake_cmd,
    ):
        await run_interactive_ask(question="что нового?")

    assert fake_cmd.call_args.kwargs["refresh"] is False
