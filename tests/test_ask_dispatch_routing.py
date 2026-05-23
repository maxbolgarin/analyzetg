"""`unread ask` pre-dispatches non-Telegram refs to the source adapters."""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

# Strip ANSI control sequences before substring checks. Rich highlights
# option flags by inserting reset codes mid-token (`--chat` renders as
# `\x1b[1;33m-\x1b[0m\x1b[1;33m-chat\x1b[0m`), so a literal `'--chat' in
# output` check fails on CI where `FORCE_COLOR=1` is set by GitHub
# Actions. Stripping ANSI first restores the plain-text invariant.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(s: str) -> str:
    return _ANSI_RE.sub("", s).lower()


@pytest.fixture
def runner():
    return CliRunner()


def test_ask_youtube_url_routes_to_youtube_adapter(runner) -> None:
    """`unread ask https://youtu.be/X "Q"` calls cmd_ask_youtube and never opens TG."""
    from unread.cli import app

    with (
        patch("unread.ask.sources.youtube.cmd_ask_youtube") as mock_yt,
        patch("unread.tg.client.tg_client") as mock_tg,
    ):

        async def _noop(*a, **kw):
            return None

        mock_yt.side_effect = _noop
        result = runner.invoke(app, ["ask", "https://youtu.be/dQw4w9WgXcQ", "What's the song about?"])
    assert result.exit_code == 0, result.output
    mock_yt.assert_called_once()
    mock_tg.assert_not_called()


def test_ask_website_url_routes_to_website_adapter(runner) -> None:
    """`unread ask https://example.com "Q"` calls cmd_ask_website."""
    from unread.cli import app

    with (
        patch("unread.ask.sources.website.cmd_ask_website") as mock_web,
        patch("unread.tg.client.tg_client") as mock_tg,
    ):

        async def _noop(*a, **kw):
            return None

        mock_web.side_effect = _noop
        result = runner.invoke(app, ["ask", "https://example.com/article", "Summarize"])
    assert result.exit_code == 0, result.output
    mock_web.assert_called_once()
    mock_tg.assert_not_called()


def test_ask_local_file_routes_to_file_adapter(runner, tmp_path) -> None:
    """`unread ask ./file.md "Q"` calls cmd_ask_file."""
    from unread.cli import app

    f = tmp_path / "notes.md"
    f.write_text("Hello world.", encoding="utf-8")

    with (
        patch("unread.ask.sources.file.cmd_ask_file") as mock_file,
        patch("unread.tg.client.tg_client") as mock_tg,
    ):

        async def _noop(*a, **kw):
            return None

        mock_file.side_effect = _noop
        result = runner.invoke(app, ["ask", str(f), "What does it say?"])
    assert result.exit_code == 0, result.output
    mock_file.assert_called_once()
    mock_tg.assert_not_called()


def test_ask_telegram_handle_still_uses_chat_archive_path(runner) -> None:
    """Sanity: `unread ask @somegroup "Q"` does NOT route to a source adapter."""
    from unread.cli import app

    with (
        patch("unread.ask.sources.youtube.cmd_ask_youtube") as mock_yt,
        patch("unread.ask.sources.website.cmd_ask_website") as mock_web,
        patch("unread.ask.sources.file.cmd_ask_file") as mock_file,
        patch("unread.ask.commands.cmd_ask") as mock_chat,
    ):

        async def _noop(*a, **kw):
            return None

        mock_chat.side_effect = _noop
        # Allow non-zero — some credential gates may fire — but no source
        # adapter should ever be called for a Telegram handle.
        runner.invoke(app, ["ask", "@somegroup", "What did Bob say?"])
    mock_yt.assert_not_called()
    mock_web.assert_not_called()
    mock_file.assert_not_called()


def test_ask_dash_ref_normalizes_to_stdin_sentinel(runner) -> None:
    """`unread ask - "Q"` routes through the file adapter with the <stdin> sentinel."""
    from unread.cli import _STDIN_REF_SENTINEL, app

    with patch("unread.ask.sources.file.cmd_ask_file") as mock_file:

        async def _noop(*a, **kw):
            return None

        mock_file.side_effect = _noop
        result = runner.invoke(app, ["ask", "-", "What does it say?"])
    assert result.exit_code == 0, result.output
    mock_file.assert_called_once()
    args, _kwargs = mock_file.call_args
    assert args[0] == _STDIN_REF_SENTINEL


def test_ask_doc_ref_with_chat_flag_is_rejected(runner, tmp_path) -> None:
    """`unread ask <file> --chat @x` errors with a 'pick one scope' message."""
    from unread.cli import app

    f = tmp_path / "notes.md"
    f.write_text("Hi.", encoding="utf-8")

    result = runner.invoke(app, ["ask", str(f), "Q", "--chat", "@somegroup"])
    assert result.exit_code != 0
    out = _plain(result.output + (result.stderr or ""))
    assert "--chat" in out
    assert "pick one scope" in out or "doc ref" in out


def test_ask_doc_ref_with_global_flag_is_rejected(runner) -> None:
    """`unread ask <youtube> --global` errors instead of silently ignoring --global."""
    from unread.cli import app

    result = runner.invoke(app, ["ask", "https://youtu.be/X", "Q", "--global"])
    assert result.exit_code != 0
    out = _plain(result.output + (result.stderr or ""))
    assert "--global" in out
