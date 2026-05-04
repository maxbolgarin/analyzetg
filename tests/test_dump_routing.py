"""`unread dump <url>` routes to non-Telegram adapters before opening tg_client.

Pre-fix: a website or YouTube URL would trip the
"Telegram not configured (api_id / api_hash missing)" banner because
`cmd_dump` opened a Telethon client for any non-`tg`/non-`None` ref.
This file pins the new behavior: file/YouTube/website refs dispatch to
their adapters and never touch Telegram.

Tests patch the inner adapter (`cmd_dump_youtube` / `cmd_dump_website`),
NOT the dispatcher helpers — so the dispatcher's validation
(Telegram-only flags, mode whitelist, --youtube-source) actually runs
in tests instead of being short-circuited.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner


def _runner() -> CliRunner:
    return CliRunner()


def test_youtube_url_dispatches_without_tg_client() -> None:
    from unread.cli import app

    with (
        patch(
            "unread.youtube.dump.cmd_dump_youtube",
            new_callable=AsyncMock,
        ) as yt_mock,
        patch(
            "unread.website.dump.cmd_dump_website",
            new_callable=AsyncMock,
        ) as web_mock,
        patch("unread.export.commands.tg_client") as tg_mock,
    ):
        result = _runner().invoke(
            app,
            ["dump", "https://youtu.be/dQw4w9WgXcQ", "--mode", "transcript"],
        )

    assert result.exit_code == 0, result.output
    yt_mock.assert_awaited_once()
    web_mock.assert_not_called()
    tg_mock.assert_not_called()


def test_website_url_dispatches_without_tg_client() -> None:
    from unread.cli import app

    with (
        patch(
            "unread.website.dump.cmd_dump_website",
            new_callable=AsyncMock,
        ) as web_mock,
        patch(
            "unread.youtube.dump.cmd_dump_youtube",
            new_callable=AsyncMock,
        ) as yt_mock,
        patch("unread.export.commands.tg_client") as tg_mock,
    ):
        result = _runner().invoke(
            app,
            ["dump", "https://example.com/article", "--mode", "text"],
        )

    assert result.exit_code == 0, result.output
    web_mock.assert_awaited_once()
    yt_mock.assert_not_called()
    tg_mock.assert_not_called()


def test_telegram_t_me_link_still_routes_to_telegram() -> None:
    """t.me URLs are NOT websites — they must still hit the Telegram path."""
    from unread.cli import app

    # Throw on call (before __aenter__) so no coroutine is left dangling.
    def _boom(*_a, **_kw):
        raise RuntimeError("tg path reached")

    with (
        patch(
            "unread.website.dump.cmd_dump_website",
            new_callable=AsyncMock,
        ) as web_mock,
        patch(
            "unread.youtube.dump.cmd_dump_youtube",
            new_callable=AsyncMock,
        ) as yt_mock,
        patch("unread.export.commands.tg_client", side_effect=_boom) as tg_mock,
    ):
        _runner().invoke(app, ["dump", "https://t.me/somechannel"])

    web_mock.assert_not_called()
    yt_mock.assert_not_called()
    tg_mock.assert_called_once()


def test_local_file_ref_routes_to_file_adapter(tmp_path) -> None:
    """`unread dump <local-file>` now routes through cmd_dump_file (was: rejected)."""
    f = tmp_path / "article.txt"
    f.write_text("hello world")

    from unread.cli import app

    with (
        patch(
            "unread.website.dump.cmd_dump_website",
            new_callable=AsyncMock,
        ) as web_mock,
        patch(
            "unread.youtube.dump.cmd_dump_youtube",
            new_callable=AsyncMock,
        ) as yt_mock,
        patch(
            "unread.files.dump.cmd_dump_file",
            new_callable=AsyncMock,
        ) as file_mock,
        patch("unread.export.commands.tg_client") as tg_mock,
    ):
        result = _runner().invoke(app, ["dump", str(f)])

    assert result.exit_code == 0, result.output
    file_mock.assert_called_once()
    web_mock.assert_not_called()
    yt_mock.assert_not_called()
    tg_mock.assert_not_called()


def test_youtube_url_rejects_telegram_only_flags() -> None:
    from unread.cli import app

    with (
        patch(
            "unread.youtube.dump.cmd_dump_youtube",
            new_callable=AsyncMock,
        ) as yt_mock,
        patch("unread.export.commands.tg_client") as tg_mock,
    ):
        result = _runner().invoke(
            app,
            [
                "dump",
                "https://youtu.be/dQw4w9WgXcQ",
                "--folder",
                "Reading",
                "--mode",
                "transcript",
            ],
        )

    assert result.exit_code != 0
    assert "--folder" in result.output
    flat = " ".join(result.output.lower().split())
    assert "telegram chats" in flat
    yt_mock.assert_not_called()
    tg_mock.assert_not_called()


def test_website_url_rejects_telegram_only_flags() -> None:
    from unread.cli import app

    with (
        patch(
            "unread.website.dump.cmd_dump_website",
            new_callable=AsyncMock,
        ) as web_mock,
        patch("unread.export.commands.tg_client") as tg_mock,
    ):
        result = _runner().invoke(
            app,
            [
                "dump",
                "https://example.com/x",
                "--since",
                "2024-01-01",
                "--mode",
                "text",
            ],
        )

    assert result.exit_code != 0
    assert "--since" in result.output
    web_mock.assert_not_called()
    tg_mock.assert_not_called()


def test_invalid_mode_for_website_kind_errors() -> None:
    from unread.cli import app

    with (
        patch(
            "unread.website.dump.cmd_dump_website",
            new_callable=AsyncMock,
        ) as web_mock,
        patch("unread.export.commands.tg_client") as tg_mock,
    ):
        result = _runner().invoke(
            app,
            ["dump", "https://example.com/x", "--mode", "video"],
        )

    assert result.exit_code != 0
    assert "--mode" in result.output
    assert "website" in result.output.lower()
    web_mock.assert_not_called()
    tg_mock.assert_not_called()


def test_invalid_mode_for_youtube_kind_errors() -> None:
    from unread.cli import app

    with (
        patch(
            "unread.youtube.dump.cmd_dump_youtube",
            new_callable=AsyncMock,
        ) as yt_mock,
        patch("unread.export.commands.tg_client") as tg_mock,
    ):
        result = _runner().invoke(
            app,
            ["dump", "https://youtu.be/abc", "--mode", "full"],
        )

    assert result.exit_code != 0
    assert "--mode" in result.output
    assert "youtube" in result.output.lower()
    yt_mock.assert_not_called()
    tg_mock.assert_not_called()


def test_youtube_url_invalid_youtube_source_errors() -> None:
    from unread.cli import app

    with (
        patch(
            "unread.youtube.dump.cmd_dump_youtube",
            new_callable=AsyncMock,
        ) as yt_mock,
        patch("unread.export.commands.tg_client") as tg_mock,
    ):
        result = _runner().invoke(
            app,
            [
                "dump",
                "https://youtu.be/abc",
                "--mode",
                "transcript",
                "--youtube-source",
                "xyz",
            ],
        )

    assert result.exit_code != 0
    assert "youtube-source" in result.output
    yt_mock.assert_not_called()
    tg_mock.assert_not_called()
