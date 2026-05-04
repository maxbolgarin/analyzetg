"""End-to-end smoke tests for the v1.0 release.

These run against a fresh `UNREAD_HOME` tmp install with a stubbed
AI provider — no network, no Telegram, no real model calls. The point
is the wiring: CLI parsing → command dispatch → settings loader →
provider routing → formatter → report write. A single broken import or
stale schema migration would surface here even if every isolated unit
test still passes.

Slow-ish (~1s each) compared to unit tests; if needed they can be
gated behind a `pytest -m e2e` marker. For now they run on every
`pytest -q` so a regression caught locally before the release dry-run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _runner() -> CliRunner:
    return CliRunner()


def _fresh_home(tmp_path: Path, monkeypatch) -> Path:
    """Pin UNREAD_HOME to a freshly-created tmp dir for one test."""
    home = tmp_path / "unread-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("UNREAD_HOME", str(home))
    from unread.config import reset_settings

    reset_settings()
    return home


def test_smoke_version_flag_exits_zero(tmp_path, monkeypatch):
    """`unread --version` is the first thing every install uses to confirm
    the binary works. Must print a version string and exit 0."""
    _fresh_home(tmp_path, monkeypatch)
    from unread.cli import app

    result = _runner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    # Version follows semver-ish; just confirm something printed.
    assert any(c.isdigit() for c in result.output), result.output


def test_smoke_help_renders(tmp_path, monkeypatch):
    """`unread --help` renders without crashing and lists core commands.

    The bootstrap at unread.cli import time runs apply_db_overrides_sync
    against UNREAD_HOME's data.sqlite; a corrupt / missing DB used to
    crash --help (pre-prod review MEDIUM). This test pins the fix.
    """
    _fresh_home(tmp_path, monkeypatch)
    from unread.cli import app

    result = _runner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    # Sanity: at least one core command must be advertised.
    out = result.output.lower()
    assert "doctor" in out or "analyze" in out or "ask" in out


def test_smoke_doctor_runs_on_fresh_install(tmp_path, monkeypatch):
    """`unread doctor` on a fresh tmp install completes (may exit 1 due
    to missing creds / ffmpeg, but must not crash with a stack trace)."""
    _fresh_home(tmp_path, monkeypatch)
    # No credentials set — doctor will report missing OpenAI key but
    # shouldn't crash. Strip the autouse fakes from conftest just for
    # this test so doctor sees a realistic empty install.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    from unread.config import reset_settings

    reset_settings()

    from unread.cli import app

    result = _runner().invoke(app, ["doctor"])
    # Exit 0 (everything OK) or 1 (warnings) both acceptable; only a
    # non-zero non-1 (i.e. crash) fails the smoke.
    assert result.exit_code in (0, 1), result.output
    # Output must include some doctor section; "ffmpeg" and "OpenAI"
    # are stable banner words.
    assert "ffmpeg" in result.output.lower() or "openai" in result.output.lower(), result.output


def test_smoke_local_file_routes_to_file_adapter(tmp_path, monkeypatch):
    """`unread <local-file>` routes to cmd_analyze_file (not Telegram).

    Wiring smoke: confirms _looks_like_local_file in cli.py picks up
    the local-path heuristic and dispatches before the Telegram check
    fires. A regression here would push every file analysis through
    `tg_client` and crash on a fresh install with no Telegram creds.
    """
    _fresh_home(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-smoke")
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    from unread.config import reset_settings

    reset_settings()

    # Stub `cmd_analyze_file` itself — we just want to confirm the
    # file-routing branch was hit, not the analyzer machinery.
    with patch("unread.files.commands.cmd_analyze_file") as mock_file_cmd:

        async def _noop(**_kw):
            return None

        mock_file_cmd.side_effect = _noop

        # And stub tg_client so a routing miss can't reach Telegram.
        with patch("unread.tg.client.tg_client") as mock_tg_ctx:
            from unread.cli import app

            result = _runner().invoke(
                app,
                [str(FIXTURES / "sample.txt"), "--no-save", "--yes"],
                catch_exceptions=False,
            )
            mock_tg_ctx.assert_not_called(), "file ref must NOT open tg_client"

    if result.exit_code != 0:
        pytest.fail(f"local-file routing smoke exited {result.exit_code} (expected 0):\n{result.output}")
    mock_file_cmd.assert_called_once()
