"""`unread prompt "..."` — direct chat with the configured AI provider.

Pins the user-facing contract:
  - No retrieval, no Telegram session — just `chat_complete` with the
    user's text and an optional answer-language system line.
  - Default = render to terminal; `--output` saves a markdown file.
  - Missing chat-provider key → first-run banner + Exit(1).
  - `--max-cost` + `--yes` exits cleanly when the estimate overshoots.
  - `phase=prompt` is the cost-log tag, so `unread stats --by kind` will
    surface the new path without code changes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from unread.ai.providers import ChatResult


def _fake_result(text: str = "hello world") -> ChatResult:
    return ChatResult(
        text=text,
        prompt_tokens=10,
        cached_tokens=0,
        completion_tokens=5,
        cost_usd=0.0001,
        truncated=False,
    )


def _patch_repo_open():
    """Stub `open_repo` so the function never touches the real DB."""
    fake_repo = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_repo)
    cm.__aexit__ = AsyncMock(return_value=False)
    return patch("unread.ai.prompt.open_repo", return_value=cm)


@pytest.mark.asyncio
async def test_prompt_sends_user_only_when_no_answer_lang() -> None:
    """No `report_language` → no system message; just the raw prompt."""
    from unread.ai import prompt as prompt_mod
    from unread.config import get_settings, reset_settings

    reset_settings()
    s = get_settings()
    s.locale.report_language = ""

    captured: dict[str, list[dict[str, str]]] = {}

    async def fake_chat_complete(provider, *, repo, model, messages, max_tokens, context):
        captured["messages"] = messages
        captured["context"] = context
        return _fake_result("answer")

    with (
        patch.object(prompt_mod, "chat_complete", new=fake_chat_complete),
        patch.object(prompt_mod, "make_chat_provider", return_value=MagicMock()),
        _patch_repo_open(),
    ):
        out = await prompt_mod.cmd_prompt(prompt="what is 2+2?")

    assert out == "answer"
    assert captured["messages"] == [{"role": "user", "content": "what is 2+2?"}]
    assert captured["context"] == {"phase": "prompt"}


@pytest.mark.asyncio
async def test_prompt_includes_language_system_line_when_set() -> None:
    """`--report-language ru` becomes a single `Respond in ru.` system message."""
    from unread.ai import prompt as prompt_mod

    captured: dict[str, list[dict[str, str]]] = {}

    async def fake_chat_complete(provider, *, repo, model, messages, max_tokens, context):
        captured["messages"] = messages
        return _fake_result("привет")

    with (
        patch.object(prompt_mod, "chat_complete", new=fake_chat_complete),
        patch.object(prompt_mod, "make_chat_provider", return_value=MagicMock()),
        _patch_repo_open(),
    ):
        await prompt_mod.cmd_prompt(prompt="hi", report_language="ru")

    assert captured["messages"][0] == {"role": "system", "content": "Respond in ru."}
    assert captured["messages"][1] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_prompt_falls_back_to_settings_locale_report_language() -> None:
    """No CLI flag, but `settings.locale.report_language` is set → still hints."""
    from unread.ai import prompt as prompt_mod
    from unread.config import get_settings, reset_settings

    reset_settings()
    s = get_settings()
    s.locale.report_language = "ru"

    captured: dict[str, list[dict[str, str]]] = {}

    async def fake_chat_complete(provider, *, repo, model, messages, max_tokens, context):
        captured["messages"] = messages
        return _fake_result("ok")

    try:
        with (
            patch.object(prompt_mod, "chat_complete", new=fake_chat_complete),
            patch.object(prompt_mod, "make_chat_provider", return_value=MagicMock()),
            _patch_repo_open(),
        ):
            await prompt_mod.cmd_prompt(prompt="hello")
        assert captured["messages"][0]["content"] == "Respond in ru."
    finally:
        reset_settings()


@pytest.mark.asyncio
async def test_prompt_writes_output_file(tmp_path) -> None:
    """`--output` saves a markdown file with title + answer body."""
    from unread.ai import prompt as prompt_mod

    out_path = tmp_path / "answer.md"

    with (
        patch.object(prompt_mod, "chat_complete", new=AsyncMock(return_value=_fake_result("42"))),
        patch.object(prompt_mod, "make_chat_provider", return_value=MagicMock()),
        _patch_repo_open(),
    ):
        await prompt_mod.cmd_prompt(prompt="what is 2+2?", output=out_path)

    body = out_path.read_text(encoding="utf-8")
    assert body.startswith("# what is 2+2?")
    assert "42" in body


def test_prompt_missing_credentials_shows_banner(monkeypatch) -> None:
    """No chat-provider key → friendly banner + exit(1); chat_complete never called."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from unread.config import reset_settings

    reset_settings()
    from unread.cli import app

    runner = CliRunner()
    with patch("unread.ai.prompt.chat_complete", new_callable=AsyncMock) as mock_call:
        result = runner.invoke(app, ["prompt", "hello"])

    assert result.exit_code == 1
    assert "OpenAI key missing" in result.output or "AI provider key missing" in result.output
    mock_call.assert_not_called()


def test_prompt_max_cost_with_yes_aborts_silently(monkeypatch) -> None:
    """`--max-cost` + `--yes` exits with code 2 when the estimate exceeds the cap."""
    from unread.cli import app

    runner = CliRunner()
    with (
        # `chat_cost` is imported lazily inside cmd_prompt; patch the source.
        patch("unread.util.pricing.chat_cost", return_value=0.01),
        patch("unread.ai.prompt.chat_complete", new_callable=AsyncMock) as mock_call,
        patch("unread.ai.prompt.make_chat_provider", return_value=MagicMock()),
    ):
        result = runner.invoke(app, ["prompt", "hi", "--max-cost", "0.0001", "--yes"])

    assert result.exit_code == 2, result.output
    mock_call.assert_not_called()


def test_prompt_help_lists_command() -> None:
    """The new command must appear in `unread --help` under the Main panel."""
    from unread.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "prompt" in result.output


def test_prompt_help_for_command_renders() -> None:
    """`unread prompt --help` renders without raising."""
    from unread.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["prompt", "--help"])

    assert result.exit_code == 0
    assert "--report-language" in result.output
    assert "--max-tokens" in result.output
