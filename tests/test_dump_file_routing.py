"""`unread dump` accepts local-file and stdin refs (parity with analyze and ask)."""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner


def _runner() -> CliRunner:
    return CliRunner()


def test_dump_local_file_routes_to_file_adapter(tmp_path) -> None:
    """`unread dump ./file.md` calls cmd_dump_file and never opens TG."""
    from unread.cli import app

    f = tmp_path / "notes.md"
    f.write_text("Hello world.", encoding="utf-8")

    with (
        patch("unread.files.dump.cmd_dump_file") as mock_dump_file,
        patch("unread.export.commands.tg_client") as mock_tg,
    ):

        async def _noop(*a, **kw):
            return None

        mock_dump_file.side_effect = _noop
        result = _runner().invoke(app, ["dump", str(f)])
    assert result.exit_code == 0, result.output
    mock_dump_file.assert_called_once()
    mock_tg.assert_not_called()


def test_dump_no_longer_rejects_local_files(tmp_path) -> None:
    """`unread dump ./file.md` succeeds (used to exit non-zero)."""
    from unread.cli import app

    f = tmp_path / "notes.md"
    f.write_text("Hello world.", encoding="utf-8")

    with patch("unread.files.dump.cmd_dump_file") as mock_dump_file:

        async def _noop(*a, **kw):
            return None

        mock_dump_file.side_effect = _noop
        result = _runner().invoke(app, ["dump", str(f)])
    assert result.exit_code == 0, result.output
    assert "already in their final form" not in result.output
