"""Tests for `unread.bot.progress` and tg.py `_pulling_status`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from unread.bot.progress import edit_progress


@dataclass
class _FakeMsg:
    edits: list[tuple[str, dict]] = field(default_factory=list)

    async def edit(self, text: str, **kwargs: Any) -> None:
        self.edits.append((text, kwargs))


@pytest.mark.asyncio
async def test_edit_progress_passes_buttons_none():
    """Every edit must clear the inline keyboard. Critical: today
    the bot leaves a stale keyboard attached to a "Pulling messages…"
    message if buttons=None isn't explicit."""
    msg = _FakeMsg()
    await edit_progress(msg, "⏳ Working…")
    assert msg.edits == [("⏳ Working…", {"buttons": None})]


@pytest.mark.asyncio
async def test_edit_progress_no_op_on_none_msg():
    """Caller-side guard — avoids `if msg is not None` everywhere."""
    # Should not raise.
    await edit_progress(None, "anything")


@pytest.mark.asyncio
async def test_edit_progress_swallows_edit_failure():
    """Status updates are best-effort — a transient edit error must
    not tear down the request that's actively analyzing."""

    @dataclass
    class _Flaky:
        async def edit(self, text: str, **kwargs: Any) -> None:
            raise RuntimeError("MESSAGE_NOT_MODIFIED")

    # Should not raise.
    await edit_progress(_Flaky(), "⏳ Still working…")


@pytest.mark.parametrize(
    "window,parsed,expected_contains",
    [
        ("msg", "81", "message `81`"),
        ("from_msg", "81", "from `81`"),
        ("1d", None, "last day"),
        ("7d", None, "last week"),
        ("30d", None, "last month"),
        (None, "81", "around `81`"),
        (None, None, "recent"),
    ],
)
def test_pulling_status_renders_window_context(window, parsed, expected_contains):
    from unread.bot.handlers.tg import _pulling_status

    text = _pulling_status(window, parsed)
    assert expected_contains in text.lower()
    assert text.startswith("⏳")
