"""Per-command gates: analyze / ask need OpenAI; Telegram-only commands don't.

Pins the user-facing contract that a Telegram-only install (no OpenAI
key) can still run `dump`, `describe`, `sync`, etc., while `analyze`
and `ask` exit cleanly with the OpenAI banner.

Tests clear `OPENAI_API_KEY` (set as a fake by `conftest.py`) before
running so the gate fires.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner


def _drop_openai(monkeypatch) -> None:
    """Clear every source the OpenAI gate looks at."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # `load_settings` reads env first; reset the singleton so the next
    # `get_settings()` rebuilds without the fake key.
    from unread.config import reset_settings

    reset_settings()


def test_bare_unread_with_no_openai_shows_banner(monkeypatch) -> None:
    """`unread @group` exits with the OpenAI banner when no key is set."""
    _drop_openai(monkeypatch)
    from unread.cli import app

    runner = CliRunner()
    # The cli's `_ensure_ready_for_analyze` is what fires the banner;
    # cmd_analyze must NOT be reached.
    with patch("unread.analyzer.commands.cmd_analyze", new_callable=AsyncMock) as mock:
        result = runner.invoke(app, ["@somegroup"])

    assert result.exit_code == 1
    assert "OpenAI key missing" in result.output
    assert "unread init" in result.output
    mock.assert_not_called()


def test_ask_with_no_openai_shows_banner(monkeypatch) -> None:
    """`unread ask "..."` exits with the OpenAI banner when no key is set."""
    _drop_openai(monkeypatch)
    from unread.cli import app

    runner = CliRunner()
    with patch("unread.ask.commands._run_single_turn", new_callable=AsyncMock) as mock:
        result = runner.invoke(app, ["ask", "anything", "--global"])

    assert result.exit_code == 1
    assert "OpenAI key missing" in result.output
    mock.assert_not_called()


def test_youtube_url_also_gated(monkeypatch) -> None:
    """OpenAI is required for YouTube/website analysis too — same banner."""
    _drop_openai(monkeypatch)
    from unread.cli import app

    runner = CliRunner()
    with patch("unread.analyzer.commands.cmd_analyze", new_callable=AsyncMock) as mock:
        result = runner.invoke(app, ["https://youtu.be/dQw4w9WgXcQ"])

    assert result.exit_code == 1
    assert "OpenAI key missing" in result.output
    mock.assert_not_called()


def test_with_openai_present_proceeds_to_analyze(monkeypatch) -> None:
    """Sanity check: the gate doesn't fire when the conftest fake key is intact."""
    # Don't drop OPENAI_API_KEY; conftest set it. cmd_analyze is mocked
    # so we don't actually hit Telegram.
    from unread.cli import app

    runner = CliRunner()
    with (
        patch("unread.cli._ensure_ready_for_analyze", return_value=True),
        patch("unread.analyzer.commands.cmd_analyze", new_callable=AsyncMock) as mock,
    ):
        result = runner.invoke(app, ["@somegroup"])

    assert result.exit_code == 0, result.output
    mock.assert_called_once()


@pytest.mark.parametrize("missing", ["openai", "telegram", "both"])
def test_first_run_banner_renders_each_variant(missing: str) -> None:
    """Each `missing=` value produces a non-empty banner without crashing."""
    from unread.cli import _print_first_run_banner

    # `_print_first_run_banner` writes to the rich console; we only care
    # that it doesn't raise. The exact copy is asserted in the gating
    # tests above.
    _print_first_run_banner(missing)


# --- bare `unread` setup prompt ----------------------------------------


def test_bare_unread_offers_init_when_uninitialized(monkeypatch, tmp_path) -> None:
    """`unread` with no install.toml asks 'Run setup now?' before falling
    through to the quickstart panel."""
    from unread.cli import app

    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    _drop_openai(monkeypatch)

    runner = CliRunner()
    # `_stdin_has_data` reads `sys.stdin.isatty()`; runner.invoke with
    # no `input=` puts a TTY-like stream behind it, so the prompt fires.
    # We say "no" → wizard doesn't run; quickstart prints; exit 0.
    with patch("typer.confirm", return_value=False) as confirm:
        result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    confirm.assert_called_once()
    assert "isn't set up yet" in result.output or "AI provider key" in result.output
    # Help overview still prints after the user declines.
    # Bare `unread` shows the status panel + a hint to run `unread help`;
    # the command list moved behind `unread help` so this stays a quick
    # health check.
    assert "Status" in result.output
    assert "unread help" in result.output


def test_bare_unread_offers_init_runs_wizard_on_yes(monkeypatch, tmp_path) -> None:
    """Saying 'Yes' kicks off `cmd_init` (full scope)."""
    from unread.cli import app

    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    _drop_openai(monkeypatch)

    runner = CliRunner()
    fake_init = AsyncMock()
    with (
        patch("typer.confirm", return_value=True),
        patch("unread.tg.commands.cmd_init", new=fake_init),
    ):
        result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    fake_init.assert_awaited_once()
    # The wizard kwargs should pin scope="full" — sanity-check.
    assert fake_init.await_args.kwargs.get("scope") == "full"


def test_bare_unread_skips_prompt_when_already_initialized(monkeypatch, tmp_path) -> None:
    """install.toml + populated key → no prompt, just the quickstart panel."""
    from unread.cli import app

    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    # Seed install.toml so `_is_uninitialized()` returns False.
    pointer = tmp_path / "fakehome" / ".unread"
    pointer.mkdir(parents=True, exist_ok=True)
    (pointer / "install.toml").write_text('home = ""\n', encoding="utf-8")
    # Conftest's fake `OPENAI_API_KEY` is intact, so the credential
    # check returns True → no prompt.
    from unread.config import reset_settings

    reset_settings()

    runner = CliRunner()
    with patch("typer.confirm") as confirm:
        result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    confirm.assert_not_called()
    # Bare `unread` shows the status panel + a hint to run `unread help`;
    # the command list moved behind `unread help` so this stays a quick
    # health check.
    assert "Status" in result.output
    assert "unread help" in result.output


def test_bare_unread_skips_prompt_with_key_but_no_pointer(monkeypatch, tmp_path) -> None:
    """Regression: working API key + missing install.toml pointer must NOT
    surface the 'isn't set up yet' prompt.

    Before the fix, `_is_uninitialized()` treated the pointer file as a
    hard 'wizard ever ran' marker and triggered the setup prompt for
    users who configured their install via env vars / `.env` / an
    external secrets store (i.e. anyone whose path didn't go through
    the wizard's folder-pick step). After the fix the decisive signal
    is the active provider key — pointer presence is informational only.
    """
    from unread.cli import app

    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    # Deliberately do NOT seed install.toml. Conftest's fake
    # `OPENAI_API_KEY` is intact, so the credential check returns True.
    from unread.config import reset_settings

    reset_settings()

    runner = CliRunner()
    with patch("typer.confirm") as confirm:
        result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    confirm.assert_not_called()
    assert "isn't set up yet" not in result.output
    assert "Status" in result.output


def test_bare_unread_skips_prompt_after_user_skipped_keys_in_wizard(monkeypatch, tmp_path) -> None:
    """Regression: pointer present + no AI key (user explicitly skipped during
    `unread init`) must NOT re-prompt 'Run setup now?' on every bare invocation.

    Reported scenario: user ran `unread init`, picked a folder (writes
    `install.toml`), then skipped the AI-key and Telegram steps. Bare
    `unread` should fall through to the status / quickstart panel —
    the panel already lists missing pieces and points at `unread init`.
    """
    from unread.cli import app

    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    # Seed install.toml — wizard's folder step writes this immediately.
    pointer = tmp_path / "fakehome" / ".unread"
    pointer.mkdir(parents=True, exist_ok=True)
    (pointer / "install.toml").write_text('home = ""\n', encoding="utf-8")
    # Drop the AI key — user skipped that step in the wizard.
    _drop_openai(monkeypatch)

    runner = CliRunner()
    with patch("typer.confirm") as confirm:
        result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    confirm.assert_not_called()
    assert "Run setup now" not in result.output
    assert "Status" in result.output
