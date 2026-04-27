"""Post-answer prompt: _ask_continue gate; --no-followup suppresses entirely."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_followup_prompt_user_says_no_does_not_invoke_loop():
    """`_ask_continue` returns False → no follow-up loop entered."""
    from unread.ask import commands as ask_commands

    fake_run_single = AsyncMock(return_value=("ok", []))
    fake_continue = AsyncMock(return_value=False)
    with (
        patch.object(ask_commands, "_run_single_turn", new=fake_run_single),
        patch.object(ask_commands, "_ask_continue", new=fake_continue),
        patch.object(ask_commands, "tg_client") as fake_tg,
        patch.object(ask_commands, "open_repo") as fake_repo,
    ):
        fake_tg.return_value.__aenter__ = AsyncMock(return_value=object())
        fake_tg.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_repo.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        fake_repo.return_value.__aexit__ = AsyncMock(return_value=False)

        await ask_commands.cmd_ask(
            question="hello",
            ref=None,
            chat=None,
            folder=None,
            global_scope=True,
            no_followup=False,
        )

    fake_continue.assert_awaited_once()
    # Single turn → no follow-up.
    assert fake_run_single.await_count == 1


@pytest.mark.asyncio
async def test_no_followup_flag_suppresses_prompt_entirely():
    """--no-followup → _ask_continue is never called."""
    from unread.ask import commands as ask_commands

    fake_run_single = AsyncMock(return_value=("ok", []))
    fake_continue = AsyncMock(return_value=False)
    with (
        patch.object(ask_commands, "_run_single_turn", new=fake_run_single),
        patch.object(ask_commands, "_ask_continue", new=fake_continue),
        patch.object(ask_commands, "tg_client") as fake_tg,
        patch.object(ask_commands, "open_repo") as fake_repo,
    ):
        fake_tg.return_value.__aenter__ = AsyncMock(return_value=object())
        fake_tg.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_repo.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        fake_repo.return_value.__aexit__ = AsyncMock(return_value=False)

        await ask_commands.cmd_ask(
            question="hello",
            ref=None,
            chat=None,
            folder=None,
            global_scope=True,
            no_followup=True,
        )

    fake_continue.assert_not_awaited()
