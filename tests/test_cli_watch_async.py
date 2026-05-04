"""Regression tests for `unread watch`'s async subprocess path.

Pre-prod review flagged that `_watch_loop` was calling `subprocess.run`
inside an `async def`, which blocks the event loop while the child runs
(potentially minutes for an `unread analyze`). The fix swaps in
`asyncio.create_subprocess_exec` and composes the child env so the
.env-loaded credentials still flow into the re-execed `unread`.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

import unread.cli as cli_mod


def test_watch_loop_source_does_not_block_event_loop() -> None:
    """Static guard: `_watch_loop` must not call the blocking `subprocess.run`.

    Cheap to maintain and catches accidental regressions when someone
    refactors and reaches for the more familiar synchronous API.
    """
    src = inspect.getsource(cli_mod._watch_loop)
    assert "subprocess.run" not in src, "watch must not call the blocking subprocess.run"
    assert "create_subprocess_exec" in src, "watch must use asyncio.create_subprocess_exec"


def test_watch_loop_uses_async_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Functional guard: assert `asyncio.create_subprocess_exec` is invoked."""

    captured: dict[str, object] = {}

    async def _fake_create_subprocess_exec(*cmd: str, env=None, **_kw):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["env"] = env
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        return proc

    # Skip the actual sleep between iterations.
    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    asyncio.run(cli_mod._watch_loop("1s", max_runs=1, inner=["analyze", "--folder", "Work"]))

    assert captured["cmd"] == ("unread", "analyze", "--folder", "Work")
    # The composed env must be a dict (never None) so the child gets the
    # explicit overlay rather than relying on the parent's os.environ.
    assert isinstance(captured["env"], dict)


def test_watch_loop_composes_dotenv_into_child_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dotenv overlay must merge into the child env (shell env wins)."""

    # Force-isolate the dotenv overlay seen by _watch_loop. We patch the
    # public helper rather than the private cache so the test stays
    # robust against the cache name changing.
    monkeypatch.setattr(
        "unread.config.dotenv_values",
        lambda: {"OPENAI_API_KEY": "from-dotenv", "OTHER_DOTENV_ONLY": "yes"},
    )
    # Real shell env value should win over the dotenv overlay.
    monkeypatch.setenv("OPENAI_API_KEY", "from-shell")

    captured: dict[str, object] = {}

    async def _fake_create_subprocess_exec(*_cmd: str, env=None, **_kw):  # type: ignore[no-untyped-def]
        captured["env"] = env
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        return proc

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    asyncio.run(cli_mod._watch_loop("1s", max_runs=1, inner=["analyze"]))

    env = captured["env"]
    assert isinstance(env, dict)
    # Dotenv-only key flows through.
    assert env.get("OTHER_DOTENV_ONLY") == "yes"
    # Shell wins on conflict.
    assert env.get("OPENAI_API_KEY") == "from-shell"
