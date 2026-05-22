"""`tg_client` auto-init flow.

When a TG-needing command hits a missing/expired session in an
interactive shell, `tg_client` calls `offer_inline_tg_init()` to ask
the user "Run `unread tg login` now and continue?". This pins:

  1. **Non-TTY** (the test default — `_can_interact` is False under
     pytest because stdin/stdout aren't a real terminal): the original
     exception path is preserved. `TelegramSessionExpired` propagates
     so the top-level `_run` catcher in cli.py renders the friendly
     banner. No prompt is ever shown — important because pytest can't
     answer one and would hang.

  2. **TTY + accept**: the inline init runs (`cmd_init` is awaited
     once with `scope="telegram_only"`), the connect retry succeeds,
     and the original command continues without seeing an exception.

  3. **TTY + decline**: the helper returns False; `tg_client` falls
     back to the historical `exit_session_expired` / `_exit_missing_*`
     exit so the banner still prints.

  4. **TTY + missing creds**: same shape as session-expired, but the
     pre-flight credentials check fires before `build_client`, so the
     `cmd_init` path is reached without ever attempting a connect.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from unread.tg.client import TelegramSessionExpired


@pytest.mark.asyncio
async def test_non_tty_session_expired_propagates_unchanged() -> None:
    """In non-TTY (CI / pytest) the auto-init offer never fires — the
    original `TelegramSessionExpired` propagates so the top-level
    catcher in `_run` (cli.py) emits the friendly banner."""
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.disconnect = AsyncMock()
    fake_client.is_user_authorized = AsyncMock(return_value=False)

    with (
        patch("unread.tg.client.build_client", return_value=fake_client),
        patch("unread.util.prompt._can_interact", return_value=False),
        patch("unread.tg.client.offer_inline_tg_init") as offer,
    ):
        from unread.tg.client import tg_client

        with pytest.raises(TelegramSessionExpired):
            async with tg_client(require_auth=True):
                pass

    offer.assert_not_called()
    fake_client.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_tty_accept_runs_inline_init_and_retries() -> None:
    """User accepts the prompt → `cmd_init(scope='telegram_only')`
    runs, then `tg_client` retries the connect and yields a working
    client. The original command sees no exception."""
    # First connect: unauthorized. Second connect (after init): authorized.
    auth_calls = [False, True]
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.disconnect = AsyncMock()
    fake_client.is_user_authorized = AsyncMock(side_effect=lambda: auth_calls.pop(0))

    init_called = AsyncMock()

    with (
        patch("unread.tg.client.build_client", return_value=fake_client),
        patch("unread.util.prompt._can_interact", return_value=True),
        patch("unread.tg.client.offer_inline_tg_init", new=AsyncMock(return_value=True)) as offer,
        patch("unread.tg.client._wipe_local_session"),
        # Don't actually touch the wizard — pretend init returned cleanly.
        patch("unread.tg.commands.cmd_init", new=init_called),
    ):
        from unread.tg.client import tg_client

        async with tg_client(require_auth=True) as client:
            assert client is fake_client

    offer.assert_awaited_once_with("session_expired")
    # Exactly one disconnect from the first failed attempt + one from
    # the context manager's `finally`.
    assert fake_client.disconnect.await_count == 2


@pytest.mark.asyncio
async def test_tty_decline_falls_back_to_exit_banner() -> None:
    """User declines → `tg_client` falls through to
    `exit_session_expired()` so the banner still prints. The exit
    surfaces as `typer.Exit(1)` (NOT `TelegramSessionExpired`) because
    the historical exit path went through `exit_session_expired`."""
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.disconnect = AsyncMock()
    fake_client.is_user_authorized = AsyncMock(return_value=False)

    with (
        patch("unread.tg.client.build_client", return_value=fake_client),
        patch("unread.util.prompt._can_interact", return_value=True),
        patch("unread.tg.client.offer_inline_tg_init", new=AsyncMock(return_value=False)),
        patch("unread.tg.client._wipe_local_session"),
    ):
        from unread.tg.client import tg_client

        with pytest.raises(typer.Exit) as exc_info:
            async with tg_client(require_auth=True):
                pass
        assert exc_info.value.exit_code == 1


@pytest.mark.asyncio
async def test_tty_missing_creds_offers_init_then_retries(monkeypatch) -> None:
    """`tg_client` pre-flight catches missing api_id / api_hash before
    `build_client`. In TTY mode it offers init; on accept the second
    pass sees the freshly-written creds and proceeds."""
    # Simulate "first call: blank creds; second call: populated".
    blank_settings = MagicMock()
    blank_settings.telegram.api_id = ""
    blank_settings.telegram.api_hash = ""
    populated_settings = MagicMock()
    populated_settings.telegram.api_id = "123"
    populated_settings.telegram.api_hash = "abc"

    settings_seq = [blank_settings, populated_settings]

    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.disconnect = AsyncMock()
    fake_client.is_user_authorized = AsyncMock(return_value=True)

    with (
        patch("unread.tg.client.get_settings", side_effect=lambda: settings_seq.pop(0)),
        patch("unread.tg.client.build_client", return_value=fake_client),
        patch("unread.util.prompt._can_interact", return_value=True),
        patch("unread.tg.client.offer_inline_tg_init", new=AsyncMock(return_value=True)) as offer,
    ):
        from unread.tg.client import tg_client

        async with tg_client(require_auth=True) as client:
            assert client is fake_client

    offer.assert_awaited_once_with("missing_creds")


@pytest.mark.asyncio
async def test_offer_inline_tg_init_decline_returns_false() -> None:
    """`offer_inline_tg_init` returns False when the user says no — no
    side effects (no `cmd_init`, no `_wipe_local_session`)."""
    from unread.tg import client as client_mod

    cmd_init_called = AsyncMock()
    with (
        patch("unread.util.prompt.confirm", return_value=False),
        patch("unread.tg.commands.cmd_init", new=cmd_init_called),
        patch("unread.tg.client._wipe_local_session") as wipe,
        patch("unread.config.reset_settings"),
    ):
        ok = await client_mod.offer_inline_tg_init("session_expired")

    assert ok is False
    cmd_init_called.assert_not_called()
    wipe.assert_not_called()


@pytest.mark.asyncio
async def test_offer_inline_tg_init_accept_runs_init_and_returns_true() -> None:
    """`offer_inline_tg_init` returns True after `cmd_init` finishes;
    `session_expired` reason wipes the local session first so init's
    'session already valid' short-circuit doesn't skip re-auth."""
    from unread.tg import client as client_mod

    cmd_init_called = AsyncMock()
    with (
        patch("unread.util.prompt.confirm", return_value=True),
        patch("unread.tg.commands.cmd_init", new=cmd_init_called),
        patch("unread.tg.client._wipe_local_session") as wipe,
        patch("unread.config.reset_settings") as reset,
    ):
        ok = await client_mod.offer_inline_tg_init("session_expired")

    assert ok is True
    cmd_init_called.assert_awaited_once_with(scope="telegram_only")
    wipe.assert_called_once()
    reset.assert_called_once()


@pytest.mark.asyncio
async def test_ask_wizard_preflights_tg_before_question_prompt() -> None:
    """`run_interactive_ask` runs `_ensure_tg_for_wizard` before the
    question-input prompt — so an expired-session user isn't asked to
    type a question that gets thrown away after the offer.

    Pin the call order: preflight → question prompt. If a refactor
    moves the preflight after the question, this test fails.
    """
    from unread import interactive

    call_order: list[str] = []

    async def fake_preflight() -> None:
        call_order.append("preflight")

    async def fake_collect(*args, **kwargs):
        call_order.append("collect")

    # Run with a non-empty `question` so the prompt path is short-circuited;
    # we just want to verify the preflight ran first, before _collect_answers.
    with (
        patch.object(interactive, "_ensure_tg_for_wizard", new=fake_preflight),
        patch.object(interactive, "_collect_answers", new=fake_collect),
    ):
        await interactive.run_interactive_ask(question="why is the sky blue?")

    assert call_order == ["preflight", "collect"], (
        f"Expected preflight to run before collect, got {call_order}"
    )


@pytest.mark.asyncio
async def test_offer_inline_tg_init_missing_creds_does_not_wipe_session() -> None:
    """`missing_creds` should not pre-wipe the session — there's no
    session to wipe, and wiping would surprise users who do have a
    valid session and just rotated the creds (rare, but defensible)."""
    from unread.tg import client as client_mod

    with (
        patch("unread.tg.commands.cmd_init", new=AsyncMock()),
        patch("unread.tg.client._wipe_local_session") as wipe,
        patch("unread.config.reset_settings"),
    ):
        await client_mod.offer_inline_tg_init("missing_creds")

    wipe.assert_not_called()


@pytest.mark.asyncio
async def test_offer_inline_tg_init_missing_creds_skips_confirm_and_runs_init() -> None:
    """`missing_creds` skips the inline "Run unread tg login now and continue?"
    confirm — `cmd_init`'s own "Set up Telegram login now?" step is the
    single decline gate. Asking twice is the bug we explicitly fixed."""
    from unread.tg import client as client_mod

    cmd_init_called = AsyncMock()
    confirm_called = MagicMock(return_value=True)
    with (
        patch("unread.util.prompt.confirm", new=confirm_called),
        patch("unread.tg.commands.cmd_init", new=cmd_init_called),
        patch("unread.tg.client._wipe_local_session"),
        patch("unread.config.reset_settings"),
    ):
        ok = await client_mod.offer_inline_tg_init("missing_creds")

    assert ok is True
    confirm_called.assert_not_called()
    cmd_init_called.assert_awaited_once_with(scope="telegram_only")
