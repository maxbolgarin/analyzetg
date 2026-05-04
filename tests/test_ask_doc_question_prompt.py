"""ask over a doc ref without a question: prompt on TTY, error on non-TTY."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import typer


def test_prompt_question_errors_on_non_tty() -> None:
    """Non-TTY + missing question → typer.Exit(2)."""
    from unread.ask.sources import file as file_mod

    with patch.object(file_mod, "_is_tty", return_value=False), pytest.raises(typer.Exit) as excinfo:
        file_mod._prompt_question("example.com")
    assert excinfo.value.exit_code == 2


def test_prompt_question_reads_from_input_on_tty(monkeypatch) -> None:
    """TTY + missing question → reads one line via input()."""
    from unread.ask.sources import file as file_mod

    monkeypatch.setattr(file_mod, "_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "Why is the sky blue?")
    got = file_mod._prompt_question("example.com")
    assert got == "Why is the sky blue?"
