"""`unread describe` group: callback preserves leaf behavior; folders subcommand lives under it."""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner


def test_describe_no_ref_calls_cmd_describe() -> None:
    """`unread describe` (no ref) still calls cmd_describe with ref=None."""
    from unread.cli import app

    runner = CliRunner()
    with patch("unread.tg.commands.cmd_describe") as mock_cmd:

        async def _noop(*a, **kw):
            return None

        mock_cmd.side_effect = _noop
        result = runner.invoke(app, ["describe"])
    assert result.exit_code == 0, result.output
    mock_cmd.assert_called_once()
    args, kwargs = mock_cmd.call_args
    assert (args[0] if args else kwargs.get("ref")) is None


def test_describe_with_ref_calls_cmd_describe() -> None:
    """`unread describe @somegroup` calls cmd_describe with ref='@somegroup'."""
    from unread.cli import app

    runner = CliRunner()
    with patch("unread.tg.commands.cmd_describe") as mock_cmd:

        async def _noop(*a, **kw):
            return None

        mock_cmd.side_effect = _noop
        result = runner.invoke(app, ["describe", "@somegroup"])
    assert result.exit_code == 0, result.output
    args, kwargs = mock_cmd.call_args
    assert (args[0] if args else kwargs.get("ref")) == "@somegroup"
