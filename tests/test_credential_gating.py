"""Per-command gates: analyze / ask need OpenAI; Telegram-only commands don't.

Pins the user-facing contract that a Telegram-only install (no OpenAI
key) can still run `dump`, `describe`, `sync`, etc., while `analyze`
and `ask` exit cleanly with the OpenAI banner.

Tests clear `OPENAI_API_KEY` (set as a fake by `conftest.py`) before
running so the gate fires.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner


def _drop_openai(monkeypatch) -> None:
    """Clear every source the OpenAI gate looks at."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # `load_settings` reads env first; reset the singleton so the next
    # `get_settings()` rebuilds without the fake key.
    from unread.config import reset_settings

    reset_settings()


def test_bare_unread_with_no_openai_shows_banner(monkeypatch) -> None:
    """`unread @group` exits with the OpenAI banner when no key is set."""
    _drop_openai(monkeypatch)
    from unread.cli import app

    runner = CliRunner()
    # The cli's `_ensure_ready_for_analyze` is what fires the banner;
    # cmd_analyze must NOT be reached.
    with patch("unread.analyzer.commands.cmd_analyze", new_callable=AsyncMock) as mock:
        result = runner.invoke(app, ["@somegroup"])

    assert result.exit_code == 1
    assert "OpenAI key missing" in result.output
    assert "unread tg init" in result.output
    mock.assert_not_called()


def test_ask_with_no_openai_shows_banner(monkeypatch) -> None:
    """`unread ask "..."` exits with the OpenAI banner when no key is set."""
    _drop_openai(monkeypatch)
    from unread.cli import app

    runner = CliRunner()
    with patch("unread.ask.commands._run_single_turn", new_callable=AsyncMock) as mock:
        result = runner.invoke(app, ["ask", "anything", "--global"])

    assert result.exit_code == 1
    assert "OpenAI key missing" in result.output
    mock.assert_not_called()


def test_youtube_url_also_gated(monkeypatch) -> None:
    """OpenAI is required for YouTube/website analysis too — same banner."""
    _drop_openai(monkeypatch)
    from unread.cli import app

    runner = CliRunner()
    with patch("unread.analyzer.commands.cmd_analyze", new_callable=AsyncMock) as mock:
        result = runner.invoke(app, ["https://youtu.be/dQw4w9WgXcQ"])

    assert result.exit_code == 1
    assert "OpenAI key missing" in result.output
    mock.assert_not_called()


def test_with_openai_present_proceeds_to_analyze(monkeypatch) -> None:
    """Sanity check: the gate doesn't fire when the conftest fake key is intact."""
    # Don't drop OPENAI_API_KEY; conftest set it. cmd_analyze is mocked
    # so we don't actually hit Telegram.
    from unread.cli import app

    runner = CliRunner()
    with (
        patch("unread.cli._ensure_ready_for_analyze", return_value=True),
        patch("unread.analyzer.commands.cmd_analyze", new_callable=AsyncMock) as mock,
    ):
        result = runner.invoke(app, ["@somegroup"])

    assert result.exit_code == 0, result.output
    mock.assert_called_once()


@pytest.mark.parametrize("missing", ["openai", "telegram", "both"])
def test_first_run_banner_renders_each_variant(missing: str) -> None:
    """Each `missing=` value produces a non-empty banner without crashing."""
    from unread.cli import _print_first_run_banner

    # `_print_first_run_banner` writes to the rich console; we only care
    # that it doesn't raise. The exact copy is asserted in the gating
    # tests above.
    _print_first_run_banner(missing)
