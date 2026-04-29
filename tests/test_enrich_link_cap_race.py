"""Regression test: link-enricher cap is enforced under concurrency.

Without the lock around the cap check + reservation, multiple concurrent
`handle()` tasks would all observe `counted["link"] < cap` before any of
them incremented the counter (the increment was post-await), so the
configured cap could be silently exceeded by the active concurrency
factor. We assert that with N=5 messages and cap=2, exactly 2 fetches
fire — not 5.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from unread.enrich.base import EnrichOpts
from unread.enrich.pipeline import enrich_messages
from unread.models import Message


def _msg(msg_id: int, text: str) -> Message:
    return Message(
        chat_id=-100,
        msg_id=msg_id,
        date=datetime.now(UTC),
        text=text,
    )


@pytest.mark.asyncio
async def test_link_cap_holds_under_concurrency(monkeypatch):
    msgs = [_msg(i, f"see https://example{i}.com/page") for i in range(1, 6)]
    cap = 2

    fetch_calls = 0

    async def fake_enrich_message_links(msg, **kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        # Hold the await so all 5 tasks would race past the cap check
        # without the lock.
        await asyncio.sleep(0.05)
        return [(f"https://example{msg.msg_id}.com/page", "summary")]

    opts = EnrichOpts(link=True, max_link_fetches_per_run=cap, concurrency=5)

    with patch(
        "unread.enrich.pipeline.enrich_message_links",
        side_effect=fake_enrich_message_links,
    ):
        repo = AsyncMock()
        await enrich_messages(msgs, client=None, repo=repo, opts=opts)

    assert fetch_calls == cap, f"expected {cap} fetches, got {fetch_calls}"
