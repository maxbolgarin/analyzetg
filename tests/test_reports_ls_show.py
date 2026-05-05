"""`unread reports ls` and `unread reports show` — listing + rendering UX."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner


def _runner() -> CliRunner:
    return CliRunner()


def _seed_reports(home: Path) -> Path:
    """Create a minimal reports tree under `<home>/reports/` and return the root."""
    root = home / "reports"
    (root / "website" / "example-com").mkdir(parents=True)
    (root / "youtube" / "channel").mkdir(parents=True)
    (root / ".trash" / "old").mkdir(parents=True)
    (root / "website" / "example-com" / "alpha-2026-05-05.md").write_text(
        "# Alpha\n\nFirst report body.\n", encoding="utf-8"
    )
    (root / "youtube" / "channel" / "beta-2026-05-05.md").write_text(
        "# Beta\n\nSecond report body.\n", encoding="utf-8"
    )
    # Trashed files must not appear in `ls` or be reachable via `show`.
    (root / ".trash" / "old" / "ancient.md").write_text("nope", encoding="utf-8")
    # Hidden dotfiles in the tree must not appear.
    (root / ".gitkeep").write_text("", encoding="utf-8")
    return root


def test_reports_ls_prints_root_and_lists_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    from unread.config import reset_settings

    reset_settings()
    _seed_reports(tmp_path)

    from unread.cli import app

    result = _runner().invoke(app, ["reports", "ls"])
    assert result.exit_code == 0, result.output
    assert "Reports root" in result.output
    # Rich may wrap a long path across lines at the runner's narrow width;
    # collapse whitespace before comparing so the assertion stays stable.
    flat = "".join(result.output.split())
    assert str(tmp_path / "reports").replace(" ", "") in flat
    assert "alpha-2026-05-05.md" in result.output
    assert "beta-2026-05-05.md" in result.output
    # An id column with 8-char ids appears for each row.
    from unread.cli import _report_id

    alpha_id = _report_id(Path("website/example-com/alpha-2026-05-05.md"))
    beta_id = _report_id(Path("youtube/channel/beta-2026-05-05.md"))
    assert alpha_id in result.output
    assert beta_id in result.output
    # `.trash/` and dotfiles must be filtered out.
    assert "ancient.md" not in result.output
    assert ".gitkeep" not in result.output


def test_reports_show_by_id(tmp_path, monkeypatch) -> None:
    """Passing the 8-char id from `ls` resolves to the right file."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    from unread.config import reset_settings

    reset_settings()
    _seed_reports(tmp_path)

    from unread.cli import _report_id, app

    beta_id = _report_id(Path("youtube/channel/beta-2026-05-05.md"))
    result = _runner().invoke(app, ["reports", "show", beta_id, "--raw"])
    assert result.exit_code == 0, result.output
    assert "Second report body." in result.output
    assert "First report body" not in result.output


def test_reports_ls_kind_filter(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    from unread.config import reset_settings

    reset_settings()
    _seed_reports(tmp_path)

    from unread.cli import app

    result = _runner().invoke(app, ["reports", "ls", "--kind", "youtube"])
    assert result.exit_code == 0, result.output
    assert "beta-2026-05-05.md" in result.output
    assert "alpha-2026-05-05.md" not in result.output


def test_reports_ls_empty_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    from unread.config import reset_settings

    reset_settings()
    # Don't create the reports tree — `ls` should say so without erroring.
    from unread.cli import app

    result = _runner().invoke(app, ["reports", "ls"])
    assert result.exit_code == 0, result.output
    assert "does not exist" in result.output


def test_reports_show_renders_markdown_by_substring(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    from unread.config import reset_settings

    reset_settings()
    _seed_reports(tmp_path)

    from unread.cli import app

    result = _runner().invoke(app, ["reports", "show", "alpha"])
    assert result.exit_code == 0, result.output
    # Rendered markdown wraps headings differently; check the body text.
    assert "First report body" in result.output


def test_reports_show_raw_dumps_unmodified(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    from unread.config import reset_settings

    reset_settings()
    _seed_reports(tmp_path)

    from unread.cli import app

    result = _runner().invoke(app, ["reports", "show", "beta", "--raw"])
    assert result.exit_code == 0, result.output
    assert "# Beta" in result.output
    assert "Second report body." in result.output


def test_reports_show_relative_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    from unread.config import reset_settings

    reset_settings()
    _seed_reports(tmp_path)

    from unread.cli import app

    result = _runner().invoke(app, ["reports", "show", "youtube/channel/beta-2026-05-05.md", "--raw"])
    assert result.exit_code == 0, result.output
    assert "Second report body." in result.output


def test_reports_show_ambiguous_lists_candidates(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    from unread.config import reset_settings

    reset_settings()
    root = _seed_reports(tmp_path)
    # Add another file matching `2026-05-05` so the substring is ambiguous.
    (root / "website" / "example-com" / "alpha-2-2026-05-05.md").write_text("x", encoding="utf-8")

    from unread.cli import app

    result = _runner().invoke(app, ["reports", "show", "2026-05-05"])
    assert result.exit_code == 2, result.output
    assert "Ambiguous" in result.output


def test_reports_show_no_match(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    from unread.config import reset_settings

    reset_settings()
    _seed_reports(tmp_path)

    from unread.cli import app

    result = _runner().invoke(app, ["reports", "show", "no-such-thing-xyz"])
    assert result.exit_code == 1, result.output
    assert "No report matches" in result.output


def test_reports_show_skips_trashed_files(tmp_path, monkeypatch) -> None:
    """A trashed report's filename must not resolve via substring match."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    from unread.config import reset_settings

    reset_settings()
    _seed_reports(tmp_path)

    from unread.cli import app

    result = _runner().invoke(app, ["reports", "show", "ancient"])
    assert result.exit_code == 1, result.output
    assert "No report matches" in result.output
