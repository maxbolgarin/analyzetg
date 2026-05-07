"""ask over a doc ref without a question: prompt on TTY, error on non-TTY."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import typer


async def test_prompt_question_errors_on_non_tty() -> None:
    """Non-TTY + missing question → typer.Exit(2)."""
    from unread.ask.sources import file as file_mod

    with patch.object(file_mod, "_is_tty", return_value=False), pytest.raises(typer.Exit) as excinfo:
        await file_mod._prompt_question("example.com")
    assert excinfo.value.exit_code == 2


async def test_prompt_question_reads_from_prompt_session_on_tty(monkeypatch) -> None:
    """TTY + missing question → reads one line via prompt_toolkit.

    The prompt switched from raw `input()` to `prompt_toolkit.PromptSession`
    so Esc / arrow keys are handled correctly. The test stubs
    `PromptSession.prompt_async` to feed a canned answer.
    """
    from unread.ask.sources import file as file_mod

    monkeypatch.setattr(file_mod, "_is_tty", lambda: True)

    class _StubSession:
        async def prompt_async(self, _prompt, key_bindings=None):
            return "Why is the sky blue?"

    import prompt_toolkit

    monkeypatch.setattr(prompt_toolkit, "PromptSession", lambda *a, **k: _StubSession())
    got = await file_mod._prompt_question("example.com")
    assert got == "Why is the sky blue?"


async def test_prompt_question_rejects_empty_submission(monkeypatch) -> None:
    """Empty submission (just Enter) → typer.Exit(2). LLM needs a real question."""
    from unread.ask.sources import file as file_mod

    monkeypatch.setattr(file_mod, "_is_tty", lambda: True)

    class _EmptySession:
        async def prompt_async(self, _prompt, key_bindings=None):
            return "   "

    import prompt_toolkit

    monkeypatch.setattr(prompt_toolkit, "PromptSession", lambda *a, **k: _EmptySession())
    with pytest.raises(typer.Exit) as excinfo:
        await file_mod._prompt_question("example.com")
    assert excinfo.value.exit_code == 2
