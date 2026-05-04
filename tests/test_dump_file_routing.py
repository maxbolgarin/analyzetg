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


def test_dump_file_preserves_original_extension(tmp_path, monkeypatch) -> None:
    """`unread dump ./content.ts` writes a `.ts` file (not `.md`) byte-for-byte."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path / "home"))
    from unread.config import reset_settings

    reset_settings()
    from unread.files.dump import cmd_dump_file

    src = tmp_path / "content.ts"
    body = "export const greet = (name: string) => `hello, ${name}`;\n"
    src.write_text(body, encoding="utf-8")

    import asyncio

    asyncio.run(cmd_dump_file(str(src)))

    reports = tmp_path / "home" / "reports" / "files" / "text"
    written = sorted(reports.iterdir())
    assert len(written) == 1
    assert written[0].suffix == ".ts"
    assert written[0].read_text(encoding="utf-8") == body
    # Filename includes a stamp so repeat-dumps don't overwrite.
    assert "content-" in written[0].name


def test_dump_file_does_not_wrap_in_markdown(tmp_path, monkeypatch) -> None:
    """The dumped file is the ORIGINAL bytes — no metadata header injected."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path / "home"))
    from unread.config import reset_settings

    reset_settings()
    from unread.files.dump import cmd_dump_file

    src = tmp_path / "data.json"
    body = '{"k": "v"}\n'
    src.write_text(body, encoding="utf-8")

    import asyncio

    asyncio.run(cmd_dump_file(str(src)))

    reports = tmp_path / "home" / "reports" / "files" / "text"
    written = sorted(reports.iterdir())[0]
    assert written.read_text(encoding="utf-8") == body
    # No metadata header sneaking in.
    assert "_Kind:" not in written.read_text(encoding="utf-8")


def test_dump_stdin_writes_txt(tmp_path, monkeypatch) -> None:
    """Stdin dump → ~/.unread/reports/files/stdin/<slug>-<stamp>.txt."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path / "home"))
    from unread.config import reset_settings

    reset_settings()
    from unread.files.dump import cmd_dump_file

    body = b"raw stdin bytes\n"
    with patch("unread.files.commands._read_stdin_bytes", return_value=(body, False)):
        import asyncio

        asyncio.run(cmd_dump_file("<stdin>"))

    reports = tmp_path / "home" / "reports" / "files" / "stdin"
    written = sorted(reports.iterdir())
    assert len(written) == 1
    assert written[0].suffix == ".txt"
    assert written[0].read_bytes() == body
