"""`unread tg describe` group: callback preserves leaf behavior; folders subcommand lives under it.

Post-tg-namespace move: `describe` and its `folders` child sit under
the `tg` subgroup. The legacy bare `unread describe` form is gone —
Telegram-only verbs are now namespaced under `tg`.
"""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner


def test_describe_no_ref_calls_cmd_describe() -> None:
    """`unread tg describe` (no ref) still calls cmd_describe with ref=None."""
    from unread.cli import app

    runner = CliRunner()
    with patch("unread.tg.commands.cmd_describe") as mock_cmd:

        async def _noop(*a, **kw):
            return None

        mock_cmd.side_effect = _noop
        result = runner.invoke(app, ["tg", "describe"])
    assert result.exit_code == 0, result.output
    mock_cmd.assert_called_once()
    args, kwargs = mock_cmd.call_args
    assert (args[0] if args else kwargs.get("ref")) is None


def test_describe_with_ref_calls_cmd_describe() -> None:
    """`unread tg describe @somegroup` calls cmd_describe with ref='@somegroup'."""
    from unread.cli import app

    runner = CliRunner()
    with patch("unread.tg.commands.cmd_describe") as mock_cmd:

        async def _noop(*a, **kw):
            return None

        mock_cmd.side_effect = _noop
        result = runner.invoke(app, ["tg", "describe", "@somegroup"])
    assert result.exit_code == 0, result.output
    mock_cmd.assert_called_once()
    args, kwargs = mock_cmd.call_args
    assert (args[0] if args else kwargs.get("ref")) == "@somegroup"


def test_describe_folders_calls_list_folders() -> None:
    """`unread tg describe folders` runs the folder listing helper."""
    from unread.cli import app

    runner = CliRunner()
    with patch("unread.cli._list_folders") as mock_list:

        async def _noop(*a, **kw):
            return None

        mock_list.side_effect = _noop
        result = runner.invoke(app, ["tg", "describe", "folders"])
    assert result.exit_code == 0, result.output
    mock_list.assert_called_once()


def test_top_level_folders_command_is_gone() -> None:
    """`unread folders` no longer resolves to a command."""
    from unread.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["folders"])
    # `unread folders` previously ran the folder listing. After the move
    # it must NOT silently succeed; either Click rejects it as no-such-
    # command, or the analyze entry point rejects it via _exit_unrecognized_ref.
    assert result.exit_code != 0, f"`unread folders` unexpectedly succeeded:\n{result.output}"


def test_top_level_describe_command_is_gone() -> None:
    """`unread describe` (no `tg` prefix) no longer resolves — it's `unread tg describe` now."""
    from unread.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["describe"])
    assert result.exit_code != 0, f"`unread describe` unexpectedly succeeded:\n{result.output}"
