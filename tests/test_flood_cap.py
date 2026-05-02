"""Pre-prod regressions on `unread/util/flood.py`:

* `retry_on_flood` caps any single FloodWait sleep at
  `_MAX_FLOOD_WAIT_SEC` so a 24h ban doesn't silently freeze the run.
* `RateLimiter.acquire` is concurrency-safe (asyncio.Lock around the
  rolling-bucket rebuild + append).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest


class _FakeFloodWaitError(Exception):
    """Stand-in for `telethon.errors.rpcerrorlist.FloodWaitError`.

    The real class is loaded lazily inside `retry_on_flood`. We patch
    sys.modules so the decorator's `from telethon.errors...` import
    picks up our fake.
    """

    def __init__(self, seconds: int):
        super().__init__(f"FloodWait {seconds}s")
        self.seconds = seconds


def _install_fake_telethon(monkeypatch):
    import sys
    import types

    rpc = types.ModuleType("telethon.errors.rpcerrorlist")
    rpc.FloodWaitError = _FakeFloodWaitError
    errors = types.ModuleType("telethon.errors")
    errors.rpcerrorlist = rpc
    telethon = sys.modules.get("telethon") or types.ModuleType("telethon")
    telethon.errors = errors
    monkeypatch.setitem(sys.modules, "telethon", telethon)
    monkeypatch.setitem(sys.modules, "telethon.errors", errors)
    monkeypatch.setitem(sys.modules, "telethon.errors.rpcerrorlist", rpc)


@pytest.mark.asyncio
async def test_flood_cap_raises_for_oversize_wait(monkeypatch):
    """A 24h FloodWait must convert to RuntimeError immediately, not
    silently sleep for hours."""
    _install_fake_telethon(monkeypatch)
    # Patch sleep so the test never actually waits.
    monkeypatch.setattr("unread.util.flood.asyncio.sleep", AsyncMock())

    from unread.util.flood import retry_on_flood

    @retry_on_flood(max_retries=3)
    async def always_floods() -> None:
        raise _FakeFloodWaitError(seconds=86_400)  # 24h

    with pytest.raises(RuntimeError, match=r"exceeds.*cap"):
        await always_floods()


@pytest.mark.asyncio
async def test_flood_cap_allows_short_waits(monkeypatch):
    """Sleeps under the cap should still be honored normally."""
    _install_fake_telethon(monkeypatch)
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    monkeypatch.setattr("unread.util.flood.asyncio.sleep", fake_sleep)

    from unread.util.flood import retry_on_flood

    state: dict[str, Any] = {"calls": 0}

    @retry_on_flood(max_retries=3)
    async def flood_once_then_succeed() -> str:
        state["calls"] += 1
        if state["calls"] == 1:
            raise _FakeFloodWaitError(seconds=5)
        return "ok"

    result = await flood_once_then_succeed()
    assert result == "ok"
    assert sleep_calls == [6]  # 5 + 1


@pytest.mark.asyncio
async def test_ratelimiter_serializes_concurrent_acquire():
    """Without the asyncio.Lock, two coroutines could each rebuild
    `_hits` and `append`, letting more requests through than `max`.
    With the lock, the bucket count never exceeds `max`."""
    from unread.util.flood import RateLimiter

    rl = RateLimiter(max_per_minute=2)
    # Three concurrent acquires; the third must wait for the bucket
    # to drain rather than over-acquire. Patch `asyncio.sleep` to a
    # no-op so the test runs instantly.
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    import unread.util.flood as flood_mod

    orig_sleep = flood_mod.asyncio.sleep
    flood_mod.asyncio.sleep = fake_sleep
    try:
        await asyncio.gather(rl.acquire(), rl.acquire(), rl.acquire())
    finally:
        flood_mod.asyncio.sleep = orig_sleep

    # 3 acquires with a max=2 bucket → exactly one sleep call.
    assert len(sleeps) == 1
