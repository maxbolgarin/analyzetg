"""Cover the final-attempt failure path in `unread.util.flood.retry_on_flood`.

The decorator retries `FloodWaitError` up to `max_retries` times. The
final attempt (after the loop) used to bubble the raw Telethon
exception, which crashed `unread chats run` with a confusing traceback
that bypassed the per-subscription error handler in runner.py. The fix
converts a final-attempt FloodWait into a friendly RuntimeError so the
runner can attribute the error to the offending chat instead.
"""

from __future__ import annotations

import pytest

from unread.util.flood import retry_on_flood


class _FakeFloodWait(Exception):
    """Stand-in for telethon's `FloodWaitError`.

    The decorator imports `FloodWaitError` lazily at call time so we
    monkeypatch the module attribute below to point at this class.
    """

    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood wait {seconds}s")
        self.seconds = seconds


@pytest.fixture(autouse=True)
def _patch_flood_error(monkeypatch):
    """Replace telethon's FloodWaitError with our fake.

    The decorator does `from telethon.errors.rpcerrorlist import FloodWaitError`
    inside its inner. We patch the rpcerrorlist module so the fake matches
    the `except` clause shape. If telethon isn't installed in the test env,
    we register a synthetic module so the import succeeds.
    """
    import sys
    import types

    pkg_name = "telethon.errors.rpcerrorlist"
    if pkg_name not in sys.modules:
        # Build the minimum chain of stub modules.
        for parent in ("telethon", "telethon.errors"):
            if parent not in sys.modules:
                sys.modules[parent] = types.ModuleType(parent)
        mod = types.ModuleType(pkg_name)
        sys.modules[pkg_name] = mod
    sys.modules[pkg_name].FloodWaitError = _FakeFloodWait
    yield


async def test_final_attempt_converts_to_runtime_error(monkeypatch):
    """When every retry hits FloodWait, the final attempt's FloodWait
    becomes a RuntimeError with a "rate-limited" message — not a raw
    telethon exception that bubbles to the user as a stacktrace.
    """
    # Stub asyncio.sleep so the decorator's between-retry backoff
    # (seconds=42 → ~43s * max_retries) doesn't slow the test.
    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    @retry_on_flood(max_retries=2)
    async def always_floods():
        raise _FakeFloodWait(seconds=42)

    with pytest.raises(RuntimeError, match="rate-limited"):
        await always_floods()


async def test_success_inside_retry_window_returns_value(monkeypatch):
    """Sanity: if the function succeeds before max_retries is exhausted,
    the decorator returns the value and never sleeps further.
    """
    calls = {"n": 0}

    @retry_on_flood(max_retries=3)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _FakeFloodWait(seconds=0)  # 0s sleep keeps test fast
        return "ok"

    # Stub asyncio.sleep so the 0s "+1" delay still doesn't slow the test.
    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)
    assert await flaky() == "ok"
    assert calls["n"] == 2


async def test_non_flood_exception_propagates(monkeypatch):
    """The decorator must not swallow exceptions other than FloodWait.

    A `ValueError` from the wrapped function escapes immediately so the
    caller sees the actual bug, not a retry storm.
    """

    @retry_on_flood(max_retries=5)
    async def explodes():
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        await explodes()


async def _no_sleep(_seconds):
    """Drop-in for asyncio.sleep that returns immediately."""
    return None
