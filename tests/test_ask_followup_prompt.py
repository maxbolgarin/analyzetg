"""Post-answer prompt: typer.confirm with default=False; --no-followup suppresses."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_followup_prompt_default_n_does_not_invoke_loop():
    """User presses Enter on the [y/N] prompt → no follow-up loop entered."""
    from analyzetg.ask import commands as ask_commands

    fake_run_single = AsyncMock(return_value=("ok", []))
    with (
        patch.object(ask_commands, "_run_single_turn", new=fake_run_single),
        patch.object(ask_commands.typer, "confirm", return_value=False) as fake_confirm,
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

    fake_confirm.assert_called_once()
    assert fake_confirm.call_args.kwargs.get("default") is False
    # _run_single_turn should be called exactly once (no follow-up).
    assert fake_run_single.await_count == 1


@pytest.mark.asyncio
async def test_no_followup_flag_suppresses_prompt_entirely():
    """--no-followup → typer.confirm is never called."""
    from analyzetg.ask import commands as ask_commands

    fake_run_single = AsyncMock(return_value=("ok", []))
    with (
        patch.object(ask_commands, "_run_single_turn", new=fake_run_single),
        patch.object(ask_commands.typer, "confirm", return_value=False) as fake_confirm,
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

    fake_confirm.assert_not_called()
