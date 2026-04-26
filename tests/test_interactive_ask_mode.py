"""ask-mode wizard: chat -> comments? -> thread -> period -> confirm.

The ask wizard reuses `_collect_answers` with `mode="ask"`. Compared to
analyze/dump it skips preset, enrich, output, and mark_read steps — the
ask command supplies its own question + retrieval scope on the side.

Key invariants exercised here:
  - `ALL_LOCAL` sentinel from `_pick_chat` flips `run_on_all_local=True`
    and short-circuits straight to the period step (no thread/comments).
  - A specific chat goes chat -> period -> confirm with no preset / no
    enrich / no output prompt.
  - The returned `InteractiveAnswers.preset` stays None (the field is
    optional in ask mode); callers must not assume "summary" when ask
    mode is in play.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from analyzetg.interactive import ALL_LOCAL, BACK, _collect_answers


def _stub_bind_escape_run():
    """Stub for `_bind_escape` used by the inline confirm step.

    The confirm step builds a `questionary.select(...)` call, wraps it in
    `_bind_escape` (which adds the ESC key binding), then awaits
    `.ask_async()`. By replacing `_bind_escape` itself we avoid having to
    fake the prompt-toolkit `Application` object — and since ask mode's
    flow has only one `_bind_escape` site (the confirm), nothing else is
    affected.
    """

    class _Stub:
        def ask_async(self):
            async def _run():
                return "run"

            return _run()

    def _bind(question, value):  # mirror real signature
        del question, value
        return _Stub()

    return _bind


@pytest.mark.asyncio
async def test_ask_mode_picks_all_local_skips_thread_and_enrich():
    """ALL_LOCAL -> no thread/enrich/preset; jump from chat to period to confirm."""

    async def fake_pick_chat(*a, **kw):
        # Sanity check: ask mode must request the ALL_LOCAL row.
        assert kw.get("offer_all_local") is True
        assert kw.get("offer_all_unread") is False
        return ALL_LOCAL

    async def fake_pick_period(*a, **kw):
        return ("unread", None, None, None)

    with (
        patch("analyzetg.interactive._pick_chat", new=fake_pick_chat),
        patch("analyzetg.interactive._pick_period", new=fake_pick_period),
        patch(
            "analyzetg.interactive._fetch_period_counts",
            new=AsyncMock(return_value={}),
        ),
        patch("analyzetg.interactive._bind_escape", new=_stub_bind_escape_run()),
        patch("analyzetg.interactive.tg_client") as fake_tg,
        patch("analyzetg.interactive.open_repo") as fake_repo,
    ):
        fake_tg.return_value.__aenter__ = AsyncMock(return_value=object())
        fake_tg.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_repo.return_value.__aenter__ = AsyncMock(return_value=object())
        fake_repo.return_value.__aexit__ = AsyncMock(return_value=False)

        answers = await _collect_answers(
            mode="ask",
            console_out=False,
            output=None,
            save_default=False,
            mark_read=None,
        )

    assert answers is not None
    assert answers.run_on_all_local is True
    # No specific chat picked: chat_ref / chat_kind stay empty (mirrors
    # the existing run_on_all_unread shape).
    assert answers.chat_ref == ""
    assert answers.chat_kind == ""
    assert answers.preset is None
    assert answers.enrich_kinds is None
    assert answers.period == "unread"


@pytest.mark.asyncio
async def test_ask_mode_picks_specific_chat_skips_preset_and_enrich():
    """A real chat pick goes chat -> period -> confirm (no preset, no enrich)."""
    chat = {
        "chat_id": 12345,
        "kind": "private",
        "title": "Bob",
        "unread": 3,
        "read_inbox_max_id": 0,
    }

    async def fake_pick_chat(*a, **kw):
        return chat

    async def fake_pick_period(*a, **kw):
        return ("last7", None, None, None)

    with (
        patch("analyzetg.interactive._pick_chat", new=fake_pick_chat),
        patch("analyzetg.interactive._pick_period", new=fake_pick_period),
        patch(
            "analyzetg.interactive._fetch_period_counts",
            new=AsyncMock(return_value={"last7": 10}),
        ),
        patch("analyzetg.interactive._bind_escape", new=_stub_bind_escape_run()),
        patch("analyzetg.interactive.tg_client") as fake_tg,
        patch("analyzetg.interactive.open_repo") as fake_repo,
    ):
        fake_tg.return_value.__aenter__ = AsyncMock(return_value=object())
        fake_tg.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_repo.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        fake_repo.return_value.__aexit__ = AsyncMock(return_value=False)

        answers = await _collect_answers(
            mode="ask",
            console_out=False,
            output=None,
            save_default=False,
            mark_read=None,
        )

    assert answers is not None
    assert answers.run_on_all_local is False
    # `chat` was a private chat -> no thread, no comments step.
    assert answers.chat_ref == "12345"
    assert answers.chat_kind == "private"
    assert answers.thread_id is None
    assert answers.period == "last7"
    assert answers.preset is None
    assert answers.enrich_kinds is None


@pytest.mark.asyncio
async def test_ask_mode_back_from_period_after_all_local_resets_flag():
    """User picks ALL_LOCAL, BACKs at period, picks a real chat: flag clears.

    Regression: stale `run_on_all_local` from the first chat pick used to
    bleed through into the second pick because the chat step never reset
    the flag. Result: `chat_ref` was correctly set from the second pick,
    but `run_on_all_local` was still True, causing the caller to use the
    --global path even though the user had narrowed to a single chat.
    """
    chat = {
        "chat_id": 999,
        "kind": "private",
        "title": "Alice",
        "unread": 0,
        "read_inbox_max_id": 0,
    }

    pick_chat_calls = {"n": 0}

    async def fake_pick_chat(*a, **kw):
        pick_chat_calls["n"] += 1
        # First time: pick ALL_LOCAL (sets run_on_all_local=True, jumps to period).
        # Second time (after BACK from period): pick a real chat.
        if pick_chat_calls["n"] == 1:
            return ALL_LOCAL
        return chat

    pick_period_calls = {"n": 0}

    async def fake_pick_period(*a, **kw):
        pick_period_calls["n"] += 1
        # First time at period: BACK out (returns user to chat picker).
        # Second time: pick a real period.
        if pick_period_calls["n"] == 1:
            return BACK
        return ("last7", None, None, None)

    with (
        patch("analyzetg.interactive._pick_chat", new=fake_pick_chat),
        patch("analyzetg.interactive._pick_period", new=fake_pick_period),
        patch(
            "analyzetg.interactive._fetch_period_counts",
            new=AsyncMock(return_value={}),
        ),
        patch("analyzetg.interactive._bind_escape", new=_stub_bind_escape_run()),
        patch("analyzetg.interactive.tg_client") as fake_tg,
        patch("analyzetg.interactive.open_repo") as fake_repo,
    ):
        fake_tg.return_value.__aenter__ = AsyncMock(return_value=object())
        fake_tg.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_repo.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        fake_repo.return_value.__aexit__ = AsyncMock(return_value=False)

        answers = await _collect_answers(
            mode="ask",
            console_out=False,
            output=None,
            save_default=False,
            mark_read=None,
        )

    assert answers is not None
    # The bug: this used to be True because the flag was never reset
    # when the chat step re-ran.
    assert answers.run_on_all_local is False
    assert answers.chat_ref == "999"
    assert answers.chat_kind == "private"
    assert answers.period == "last7"
