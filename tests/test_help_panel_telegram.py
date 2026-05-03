# tests/test_help_panel_telegram.py
"""Help renderer groups Telegram commands under a 'Telegram' panel."""

from __future__ import annotations

from typer.testing import CliRunner


def test_panel_telegram_key_is_translated() -> None:
    """The new i18n key resolves to a non-empty string in en (default)."""
    from unread.i18n import t

    assert t("cli_panel_telegram") == "Telegram"


def test_panel_telegram_appears_in_help() -> None:
    """`unread --help` renders a 'Telegram' panel containing the moved commands."""
    from unread.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Telegram" in out
    for cmd in ("describe", "login", "logout", "chats", "sync"):
        assert cmd in out, f"command '{cmd}' missing from --help output"


def test_panel_sync_label_no_longer_present() -> None:
    """The old 'Sync & subscriptions (Telegram)' label is gone."""
    from unread.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "Sync & subscriptions" not in result.output
