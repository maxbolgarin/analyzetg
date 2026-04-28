"""Local-file / stdin analysis: routing, kind detection, extraction, paths.

The end-to-end pipeline (extract → segment → run_analysis) is covered
by the website / youtube tests via the same `run_analysis` core; this
file pins the file-specific surface:

  - `_looks_like_local_file` routing decisions (path-shape vs Telegram).
  - `detect_kind` extension classification.
  - `extract_text` / `extract_text_from_bytes` round-trips.
  - `file_report_path` layout under `~/.unread/reports/files/<kind>/...`.
  - `local_files` table round-trip via the Repo helpers.
  - `cmd_analyze_file` rejection of unknown extensions / missing files.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from unread.cli import _looks_like_local_file, _looks_like_telegram_ref
from unread.core.paths import reports_dir
from unread.files.extractors import (
    detect_kind,
    extract_text,
    extract_text_from_bytes,
)
from unread.files.paths import file_report_path

# --- routing ------------------------------------------------------------


@pytest.mark.parametrize(
    "ref,expected_file,expected_telegram",
    [
        # Path-shape tokens always route to the file analyzer.
        ("./report.pdf", True, False),
        ("../docs/foo.md", True, False),
        ("/tmp/notes.txt", True, False),
        ("~/docs/x.md", True, False),
        ("file:///abs/path.pdf", True, False),
        ("subdir/file.txt", True, False),
        # Telegram-shaped refs never look like files.
        ("@durov", False, True),
        ("My Chat Title", False, True),
        ("-1001234567890", False, True),
        # URL refs go to YouTube / website branches.
        ("https://youtu.be/xyz", False, False),
        ("https://example.com/article", False, False),
        ("https://t.me/durov/123", False, True),
    ],
)
def test_routing_table(ref: str, expected_file: bool, expected_telegram: bool) -> None:
    assert _looks_like_local_file(ref) is expected_file
    assert _looks_like_telegram_ref(ref) is expected_telegram


def test_bare_filename_with_known_ext_is_file_when_present(tmp_path: Path, monkeypatch) -> None:
    """`unread report.pdf` (bare name) routes to file iff the file exists in cwd."""
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "report.pdf"
    p.write_bytes(b"%PDF-1.4\n")  # not a valid PDF — we only stat
    assert _looks_like_local_file("report.pdf") is True
    # Same shape but the file isn't there → falls through to Telegram.
    assert _looks_like_local_file("nonexistent.pdf") is False


# --- detect_kind --------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("notes.md", "text"),
        ("README.MD", "text"),  # case-insensitive
        ("data.json", "text"),
        ("script.py", "text"),
        ("report.pdf", "pdf"),
        ("contract.docx", "docx"),
        ("recording.mp3", "audio"),
        ("song.flac", "audio"),
        ("clip.mp4", "video"),
        ("photo.jpg", "image"),
        ("avatar.webp", "image"),
        ("blob.bin", "unknown"),
        ("noext", "unknown"),
    ],
)
def test_detect_kind(name: str, expected: str) -> None:
    assert detect_kind(Path(name)) == expected


# --- text extraction ----------------------------------------------------


def test_extract_text_utf8(tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_text("# Hello\nЭто тест", encoding="utf-8")
    res = extract_text(p)
    assert "Hello" in res.text
    assert "тест" in res.text
    assert res.extra and res.extra["bytes"] == p.stat().st_size


def test_extract_text_handles_non_utf8_without_crashing(tmp_path: Path) -> None:
    """The encoding ladder is best-effort: a non-UTF-8 file decodes via
    one of the fallbacks and never raises (utf-8 with replacement is
    the last-resort). Same behavior as the Telegram-doc enricher's
    `_extract_plain`."""
    p = tmp_path / "binary-ish.txt"
    p.write_bytes(b"\xff\xfe arbitrary bytes")  # invalid as UTF-8
    res = extract_text(p)
    # Doesn't raise, returns a non-empty string. Exact decoding is
    # encoding-dependent — that's deliberately not asserted.
    assert isinstance(res.text, str)
    assert res.text  # non-empty


def test_extract_text_from_bytes_default_label() -> None:
    res = extract_text_from_bytes(b"hello world")
    assert res.text == "hello world"
    assert res.extra and res.extra["source"] == "stdin"


# --- report paths -------------------------------------------------------


def test_file_report_path_layout() -> None:
    p = file_report_path(file_id="abc1234567890def", name="My Doc.md", kind="text", preset="summary")
    rel = p.relative_to(reports_dir())
    assert rel.parts[0] == "files"
    assert rel.parts[1] == "text"  # kind subdir
    # File slug + last-6-of-id suffix + preset name appear in the basename.
    assert "my-doc" in rel.parts[2]
    assert "summary" in rel.parts[2]


def test_file_report_path_stdin_layout() -> None:
    p = file_report_path(file_id="ignored", name="stdin", kind="stdin", preset="summary")
    rel = p.relative_to(reports_dir())
    assert rel.parts[0] == "files"
    assert rel.parts[1] == "stdin"
    # Stdin filename has no slug — just the preset + timestamp.
    assert rel.parts[2].startswith("summary-")


# --- local_files repo round-trip ---------------------------------------


def test_local_files_repo_round_trip(tmp_path: Path) -> None:
    """`put_local_file` then `get_local_file` returns the same row."""
    from unread.db.repo import Repo

    async def _go() -> dict:
        repo = await Repo.open(tmp_path / "data.sqlite")
        await repo.put_local_file(
            file_id="abcd1234",
            abs_path="/tmp/x.md",
            name="x.md",
            kind="text",
            extension=".md",
            content_hash="hash1",
            paragraphs=["a", "b"],
            extract_size=100,
        )
        row = await repo.get_local_file("abcd1234")
        await repo.close()
        return row or {}

    row = asyncio.run(_go())
    assert row["abs_path"] == "/tmp/x.md"
    assert row["kind"] == "text"
    assert row["content_hash"] == "hash1"
    # paragraphs serialize as JSON
    import json

    assert json.loads(row["paragraphs_json"]) == ["a", "b"]


# --- cmd_analyze_file rejections ---------------------------------------


def test_cmd_analyze_file_rejects_unknown_extension(tmp_path: Path) -> None:
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"garbage")
    runner = CliRunner()
    from unread.cli import app

    with patch("unread.cli._ensure_ready_for_analyze", return_value=True):
        result = runner.invoke(app, [str(blob)])
    assert result.exit_code == 2
    assert "Unsupported file type" in result.output


def test_cmd_analyze_file_rejects_missing_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    from unread.cli import app

    # `./missing.txt` looks like a path (path-shape token), so the file
    # branch picks it up; resolve fails → exit 2.
    with patch("unread.cli._ensure_ready_for_analyze", return_value=True):
        result = runner.invoke(app, ["./missing.txt"])
    assert result.exit_code == 2
    assert "file not found" in result.output.lower() or "not a regular file" in result.output.lower()


def test_cmd_analyze_file_rejects_telegram_only_flags(tmp_path: Path) -> None:
    """File analysis doesn't support `--folder`, `--thread`, etc."""
    p = tmp_path / "ok.txt"
    p.write_text("hello")
    runner = CliRunner()
    from unread.cli import app

    with patch("unread.cli._ensure_ready_for_analyze", return_value=True):
        result = runner.invoke(app, [str(p), "--folder", "Work"])
    assert result.exit_code != 0
    assert "do not support" in result.output.lower() or "--folder" in result.output


# --- end-to-end (mocked LLM) -------------------------------------------


def test_cmd_analyze_file_runs_dry_run(tmp_path: Path) -> None:
    """`--dry-run` exits cleanly without making LLM calls."""
    p = tmp_path / "doc.md"
    p.write_text("First paragraph.\n\nSecond paragraph.\n")
    runner = CliRunner()
    from unread.cli import app

    with patch("unread.cli._ensure_ready_for_analyze", return_value=True):
        result = runner.invoke(app, [str(p), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert "kind=text" in result.output


def test_cmd_analyze_stdin_dry_run() -> None:
    """`unread -` reads stdin via the CliRunner's `input=` shim."""
    runner = CliRunner()
    from unread.cli import app

    with patch("unread.cli._ensure_ready_for_analyze", return_value=True):
        result = runner.invoke(app, ["-", "--dry-run"], input="Some piped content for analysis.\n")
    assert result.exit_code == 0, result.output
    assert "kind=stdin" in result.output
